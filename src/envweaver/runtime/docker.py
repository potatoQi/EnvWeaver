import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from .._util import write_json
from ..errors import RuntimeError
from ..models import Instance, RuntimeRef


class DockerBackend:
    """Docker CLI backend. Prepared Linux images must provide Python 3."""

    def __init__(self, root):
        self.root = Path(root)

    def _docker(self, *args, timeout=300):
        result = subprocess.run(["docker", *map(str, args)], capture_output=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace"))
        return result.stdout.decode(errors="replace").strip()

    def start(self, ref):
        config = dict(ref.config)
        if "archive" in config:
            self._docker("load", "--input", config["archive"], timeout=600)
        identity = "envweaver-" + uuid.uuid4().hex
        args = ["run", "-d", "--name", identity, "--label", "envweaver.managed=true"]
        for key, flag in [
            ("memory", "--memory"),
            ("cpus", "--cpus"),
            ("network", "--network"),
            ("shm_size", "--shm-size"),
            ("gpus", "--gpus"),
            ("user", "--user"),
        ]:
            if key in config:
                args += [flag, str(config[key])]
        for key, value in config.get("env", {}).items():
            args += ["--env", f"{key}={value}"]
        if config.get("guest_port"):
            token_env = config.get("token_env", "ENVWEAVER_GUEST_TOKEN")
            if not os.environ.get(token_env):
                raise ValueError(f"Set {token_env} before starting a guest service Runtime")
            args += [
                "--publish",
                f"127.0.0.1::{config['guest_port']}",
                "--env",
                f"ENVWEAVER_GUEST_TOKEN={os.environ[token_env]}",
            ]
        # Explicit user configuration; the package is not a daemon permission boundary.
        args += config.get("args", [])
        args += [ref.image, *config.get("command", [])]
        try:
            cid = self._docker(*args, timeout=config.get("startup_timeout", 120))
            info = json.loads(self._docker("inspect", cid))[0]
            if not info["State"]["Running"]:
                raise RuntimeError("Container exited during startup; provide a long-lived command")
            # Docker commit does not include mounted data. Reject mounts instead of silently losing it.
            if info["Mounts"]:
                raise RuntimeError(
                    "Persistent Runtime snapshots require no Docker volumes/bind mounts"
                )
            data = {"ref": ref.to_dict()}
            if config.get("guest_port"):
                port = info["NetworkSettings"]["Ports"][f"{config['guest_port']}/tcp"][0][
                    "HostPort"
                ]
                data.update(guest_url=f"http://127.0.0.1:{port}", token_env=token_env)
            instance = Instance(cid, "docker", str(self.root), data)
            self.exec(instance, "true", None, 15)
            if config.get("guest_port"):
                from ..transport import GuestClient

                deadline = time.monotonic() + config.get("startup_timeout", 120)
                while True:
                    try:
                        GuestClient(instance).health()
                        break
                    except Exception:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Guest control service did not become ready")
                        time.sleep(0.25)
            return instance
        except BaseException:
            try:
                self._docker("rm", "--force", identity)
            except Exception:
                pass
            raise

    def exec(self, instance, command, cwd=None, timeout=300):
        source = Path(__file__).with_name("_command.py").read_text()
        python = instance.data["ref"]["config"].get("python", "python3")
        raw = self._docker(
            "exec",
            instance.id,
            python,
            "-c",
            source,
            json.dumps({"command": command, "cwd": cwd, "timeout": timeout}),
            timeout=timeout + 15,
        )
        result = json.loads(raw)
        if "error" in result:
            raise TimeoutError(result["message"])
        return result["exit_code"], result["stdout"], result["stderr"]

    def upload(self, instance, local_path, remote_path):
        import posixpath
        import shlex

        local = Path(local_path).resolve(strict=True)
        parent = posixpath.dirname(remote_path) or "."
        code, _, error = self.exec(instance, f"mkdir -p -- {shlex.quote(parent)}")
        if code:
            raise RuntimeError(error)
        if local.is_dir():
            code, _, error = self.exec(instance, f"mkdir -p -- {shlex.quote(remote_path)}")
            if code:
                raise RuntimeError(error)
            self._docker("cp", str(local) + "/.", f"{instance.id}:{remote_path}")
        else:
            code, _, _ = self.exec(instance, f"test -d {shlex.quote(remote_path)}")
            if code == 0:
                raise IsADirectoryError(remote_path)
            self._docker("cp", str(local), f"{instance.id}:{remote_path}")
        # docker cp creates root-owned files even when the image runs as a desktop user.
        # Give the copied working tree to that user so the solving Agent can edit it.
        python = instance.data["ref"]["config"].get("python", "python3")
        owner = self._docker(
            "exec",
            instance.id,
            python,
            "-c",
            "import os; print(str(os.getuid()) + ':' + str(os.getgid()))",
        )
        self._docker("exec", "--user", "0", instance.id, "chown", "-Rh", owner, "--", remote_path)

    def download(self, instance, remote_path, local_path):
        target = Path(local_path).absolute()
        target.parent.mkdir(parents=True, exist_ok=True)
        import shlex

        code, _, _ = self.exec(instance, f"test -d {shlex.quote(remote_path)}")
        if code == 0:
            target.mkdir(parents=True, exist_ok=True)
            self._docker("cp", f"{instance.id}:{remote_path}/.", target)
        else:
            if target.is_dir():
                raise IsADirectoryError(target)
            self._docker("cp", f"{instance.id}:{remote_path}", target)

    def save(self, instance, name):
        asset = self.root / "runtimes" / uuid.uuid4().hex
        asset.mkdir(parents=True)
        tag = f"envweaver/snapshot:{asset.name}"
        config = dict(instance.data["ref"]["config"])
        config.pop("archive", None)
        # Flush filesystem buffers before the paused commit; application saves belong to the caller.
        code, _, error = self.exec(instance, "sync")
        if code:
            raise RuntimeError(error)
        changes = ["--change", "ENV ENVWEAVER_GUEST_TOKEN="] if config.get("guest_port") else []
        self._docker("commit", "--pause=true", *changes, instance.id, tag, timeout=600)
        self._docker("image", "save", "--output", asset / "image.tar", tag, timeout=600)
        config["archive"] = str(asset / "image.tar")
        ref = RuntimeRef("docker", tag, config)
        write_json(asset / "runtime.json", ref.to_dict())
        write_json(asset / "metadata.json", {"name": name})
        return ref

    def close(self, instance):
        self._docker("rm", "--force", instance.id)
