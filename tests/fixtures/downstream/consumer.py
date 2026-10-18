"""Independent test consumer. No EnvWeaver imports or startup helpers."""

import importlib.util
import json
import math
import os
import subprocess
import sys
import uuid
from pathlib import Path

assert importlib.util.find_spec("envweaver") is None
package, host = sys.argv[1:]
package = Path(package).resolve()
manifest = json.loads((package / "task.json").read_text(encoding="utf-8"))
assert manifest["version"] == 1
spec = manifest["verifier"]
entry = package / spec["entry"]
verifier_id = None
config = manifest["task_runtime_ref"]["config"]
assert config["python"] == "python3" and "startup_timeout" not in config
assert manifest["goal"][0]["type"] == "text"
assert (package / manifest["goal"][1]["path"]).is_file()
prefix = ["docker", "--host", host] if host else ["docker", "--context", "default"]
containers, volumes, checks = [], [], []


def docker(*args, check=True):
    environment = {k: v for k, v in os.environ.items() if not k.startswith("DOCKER_")}
    return subprocess.run(
        prefix + list(args),
        check=check,
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )


def start(volume=None):
    args = ["run", "-d", "--pull=never", "--name", "ew-downstream-" + uuid.uuid4().hex]
    for key, flag in {
        "memory": "--memory",
        "cpus": "--cpus",
        "network": "--network",
        "shm_size": "--shm-size",
        "user": "--user",
    }.items():
        if key in config:
            args += [flag, str(config[key])]
    for key, value in config.get("env", {}).items():
        args += ["--env", key + "=" + value]
    args += config.get("args", [])
    if volume:
        args += ["--mount", f"type=volume,source={volume},target=/tmp/csv task/input files"]
    args += [manifest["task_runtime_ref"]["image"], *config["command"]]
    cid = docker(*args).stdout.strip()
    containers.append(cid)
    for item in manifest["context"]:
        source = (package / item["source"]).resolve(strict=True)
        assert source.is_relative_to(package)
        target = item["target"]
        docker(
            "exec",
            "--user",
            "0",
            cid,
            "mkdir",
            "-p",
            target if source.is_dir() else str(Path(target).parent),
        )
        docker("cp", str(source) + ("/." if source.is_dir() else ""), cid + ":" + target)
        docker("exec", "--user", "0", cid, "chown", "-Rh", config["user"], target)
    record = Path.cwd() / (cid + ".json")
    record.write_text(
        json.dumps(
            {
                "version": 1,
                "backend": "docker",
                "host": None,
                "container_id": cid,
                "python": config.get("python", "python3"),
            }
        ),
        encoding="utf-8",
    )
    return cid, record


def start_verifier(supplied):
    for name in spec["required_env"]:
        if not supplied.get(name):
            raise ValueError("Missing required environment variable: " + name)
    ref = manifest["verifier_runtime_ref"]
    assert ref["backend"] == "docker"
    settings = ref["config"]
    args = ["run", "-d", "--pull=never", "--name", "ew-downstream-" + uuid.uuid4().hex]
    for name in ("memory", "cpus"):
        args += ["--" + name, str(settings[name])]
    for key, value in {**settings.get("env", {}), **supplied}.items():
        args += ["--env", key + "=" + value]
    # The downstream grants this trusted test verifier access to the same Docker daemon.
    # This execution-specific connection is not stored in the Task.
    args += ["--volume", "/var/run/docker.sock:/var/run/docker.sock"]
    args += [ref["image"], *settings["command"]]
    cid = docker(*args).stdout.strip()
    containers.append(cid)
    return cid


SUPERVISOR = """import subprocess, sys
try:
    sys.exit(subprocess.run(sys.argv[2:], timeout=float(sys.argv[1])).returncode)
except subprocess.TimeoutExpired:
    print('Verification timed out', file=sys.stderr)
    sys.exit(124)
"""


def invoke(record, *, dependency_missing=False):
    verifier_id = start_verifier({"CHECKER_TOKEN": "test-value"})
    folder = "/runs/" + uuid.uuid4().hex
    try:
        docker("exec", verifier_id, "mkdir", "-p", folder)
        docker("cp", str(entry.parent), verifier_id + ":" + folder + "/verifier")
        docker("cp", str(record), verifier_id + ":" + folder + "/instance.json")
        python = ["python", "-I", "-S"] if dependency_missing else ["python", "-I"]
        return docker(
            "exec",
            "--workdir",
            folder + "/verifier",
            "--env",
            "DOCKER_HOST=ssh://wrong-host",
            "--env",
            "DOCKER_CONTEXT=wrong-context",
            verifier_id,
            "python",
            "-c",
            SUPERVISOR,
            str(spec["timeout"]),
            *python,
            "run.py",
            "--instance",
            folder + "/instance.json",
            check=False,
        )
    finally:
        docker("rm", "--force", verifier_id)
        containers.remove(verifier_id)


def verify(record):
    result = invoke(record)
    if result.returncode:
        raise RuntimeError(result.stderr)
    result = json.loads(result.stdout)
    score = result["score"]
    if (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
        or not 0 <= score <= 1
    ):
        raise ValueError("Invalid score")
    json.dumps(result, allow_nan=False)
    return score


def patch(cid, encoding):
    program = (
        "import csv,json,sys\nwith open(sys.argv[1],encoding="
        + repr(encoding)
        + ",newline='') as f:\n print(json.dumps(list(csv.reader(f))))\n"
    )
    docker(
        "exec",
        cid,
        "python3",
        "-c",
        "from pathlib import Path; Path('/tmp/csv task/parser.py').write_text("
        + repr(program)
        + ")",
    )


def checked(name):
    checks.append(name)
    print(json.dumps({"passed": name}), flush=True)


try:
    try:
        start_verifier({})
    except ValueError as error:
        assert "CHECKER_TOKEN" in str(error)
    else:
        raise AssertionError("Missing required environment variable was accepted")
    verifier_id = start_verifier({"CHECKER_TOKEN": "test-value"})
    installed = docker(
        "exec", verifier_id, "python", "-c", "import envweaver; print(envweaver.__file__)"
    )
    assert "site-packages/envweaver" in installed.stdout
    assert docker("exec", verifier_id, "printenv", "CHECKER_MODE").stdout.strip() == "csv"
    checked("native verifier container with installed wheel and required configuration")
    docker("rm", "--force", verifier_id)
    containers.remove(verifier_id)
    first, first_record = start()
    assert (
        docker(
            "exec",
            first,
            "python3",
            "-c",
            "import importlib.util; assert importlib.util.find_spec('envweaver') is None",
        ).returncode
        == 0
    )
    info = json.loads(docker("inspect", first).stdout)[0]
    assert info["HostConfig"]["Memory"] == 256 * 1024 * 1024
    assert info["HostConfig"]["NanoCpus"] == 500000000
    assert info["Config"]["User"] == "1000:1000"
    assert docker("exec", first, "printenv", "TASK_SETTING").stdout.strip() == "value with spaces"
    checked("native startup, limits, environment, non-root user; no EnvWeaver in container")
    assert verify(first_record) == 0
    patch(first, "utf-8")
    assert verify(first_record) == 0.5
    patch(first, "utf-8-sig")
    assert verify(first_record) == 1
    checked("scores: broken=0, partial=0.5, fixed=1")
    second, second_record = start()
    assert verify(second_record) == 0
    assert verify(first_record) == 1
    checked("new instance is unchanged; verifier selects requested instance")
    missing = invoke(first_record, dependency_missing=True)
    assert missing.returncode != 0 and "ModuleNotFoundError" in missing.stderr
    assert not missing.stdout.strip()
    checked("missing verifier dependency is an error, not zero score")
    original_record = json.loads(first_record.read_text())
    bad = Path.cwd() / "bad.json"
    for label, record in [
        ("missing backend", {"version": 1}),
        ("unsupported version", {**original_record, "version": 99}),
        ("unknown backend", {**original_record, "backend": "unknown"}),
        (
            "missing container",
            {**original_record, "container_id": "ew-missing-" + uuid.uuid4().hex},
        ),
        ("missing guest Python", {**original_record, "python": "/missing/python"}),
    ]:
        bad.write_text(json.dumps(record))
        result = invoke(bad)
        assert result.returncode and not result.stdout.strip(), (label, result)
    bad.write_text("{broken json")
    assert invoke(bad).returncode != 0
    checked("bad records and missing container/Python fail explicitly")
    path = "/tmp/csv task/input files/BOM 数据.csv"
    docker("exec", first, "mv", path, path + ".hidden")
    result = invoke(first_record)
    assert result.returncode != 0 and "Input material is missing" in result.stderr
    docker("exec", first, "mv", path + ".hidden", path)
    checked("missing context is reported as a setup failure")
    # A submitting program crash is different from a verifier setup error.
    docker(
        "exec",
        first,
        "python3",
        "-c",
        "from pathlib import Path; Path('/tmp/csv task/parser.py').write_text('raise Exception(\"bad submission\")')",
    )
    assert verify(first_record) == 0
    patch(first, "utf-8-sig")
    checked("broken submission gives zero; verifier can subsequently recover")
    original_entry = entry.read_text()
    try:
        for value in ['{"score":true}', '{"score":2}', '{"score":NaN}', "not json"]:
            entry.write_text("print(" + repr(value) + ")")
            try:
                verify(first_record)
            except (ValueError, KeyError):
                pass
            else:
                raise AssertionError("Invalid score accepted")
    finally:
        entry.write_text(original_entry)
    checked("consumer rejects malformed verifier output")
    original_timeout = spec["timeout"]
    try:
        spec["timeout"] = 0.3
        entry.write_text("import time\ntime.sleep(30)\nprint('{\"score\":1}')")
        timed = invoke(first_record)
        assert timed.returncode == 124 and "timed out" in timed.stderr
        assert not timed.stdout.strip()
    finally:
        spec["timeout"] = original_timeout
        entry.write_text(original_entry)
    assert verify(first_record) == 1
    checked("timed-out verifier destroyed; next verification uses a fresh container")
    absent = docker("run", "--rm", "--pull=never", "sha256:" + "0" * 64, check=False)
    assert absent.returncode != 0
    checked("missing image fails without pulling a replacement")
    docker("stop", "--time", "1", second)
    result = invoke(second_record)
    assert result.returncode != 0 and "not running" in result.stderr
    assert verify(first_record) == 1
    checked("stopped instance errors; another instance remains usable")
    volume = "ew-downstream-" + uuid.uuid4().hex
    docker("volume", "create", volume)
    volumes.append(volume)
    mounted, mounted_record = start(volume=volume)
    patch(mounted, "utf-8-sig")
    assert verify(mounted_record) == 1
    checked("verification works with downstream-mounted input materials")
    print(json.dumps({"completed": checks}), flush=True)
finally:
    failures = []
    for cid in containers:
        result = docker("rm", "--force", cid, check=False)
        if result.returncode:
            failures.append(result.stderr)
    for volume in volumes:
        result = docker("volume", "rm", volume, check=False)
        if result.returncode:
            failures.append(result.stderr)
    assert not failures, failures
