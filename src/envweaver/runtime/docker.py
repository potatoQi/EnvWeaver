import json
import os
import posixpath
import shlex
import subprocess
import uuid
from functools import partial
from pathlib import Path

from .._paths import normalize_address
from .._ssh_config import docker_environment
from .._ssh_config import normalize as normalize_ssh
from .._ssh_transport import _validate_host
from .._storage import write_records
from ..errors import RuntimeError
from ..models import Instance, RuntimeRef
from ._connection import fixed_connection, merge_connection


class DockerBackend:
    """Docker CLI backend. Prepared Linux images must provide Python 3."""

    def __init__(
        self, root: str | Path | None = None, *, host: str | None = None, ssh: dict | None = None
    ):
        self.root = normalize_address(root) if root is not None else None
        self.host = _validate_host(host)
        self.ssh = normalize_ssh(ssh)

    def _docker(self, *args, host=None, timeout=300, ssh=None):
        host = _validate_host(host)
        connection = ["--host", host] if host else ["--context", "default"]
        env = os.environ.copy()
        for key in (
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_TLS",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        ):
            env.pop(key, None)
        options = self.ssh if ssh is None else ssh
        with docker_environment(env, options if host else {}) as env:
            result = subprocess.run(
                ["docker", *connection, *map(str, args)],
                capture_output=True,
                timeout=timeout,
                env=env,
            )
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace"))
        return result.stdout.decode(errors="replace").strip()

    def create(
        self, image: str, config: dict | None = None, connection: dict | None = None
    ) -> RuntimeRef:
        connection = fixed_connection("docker", connection)
        if set(config or {}) & {"guest_port", "token_env"}:
            raise ValueError("guest_port/token_env are no longer used; use a native VNC connection")
        image_id = self._docker("image", "inspect", "--format", "{{.Id}}", image, host=self.host)
        return RuntimeRef("docker", image_id, dict(config or {}), connection)

    def start(self, ref: RuntimeRef, connection: dict | None = None) -> Instance:
        connection = merge_connection(ref.connection, connection)
        docker = partial(self._docker, host=self.host)
        config = dict(ref.config)
        if set(config) & {"guest_port", "token_env"}:
            raise ValueError("Use connection.vnc instead of guest_port/token_env")
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
        # Explicit user configuration; the package is not a daemon permission boundary.
        args += config.get("args", [])
        args += [ref.image, *config.get("command", [])]
        try:
            cid = docker(*args, timeout=config.get("startup_timeout", 120))
            info = json.loads(docker("inspect", cid))[0]
            if not info["State"]["Running"]:
                raise RuntimeError("Container exited during startup; provide a long-lived command")
            # Docker commit does not include mounted data. Reject mounts instead of silently losing it.
            if info["Mounts"] or info.get("HostConfig", {}).get("Tmpfs"):
                raise RuntimeError(
                    "Persistent Runtime snapshots require no Docker volumes/bind mounts"
                )
            data = {
                "ref": ref.to_dict(),
                "host_ssh": self.ssh,
                "python": config.get("python", "python3"),
            }
            if (connection or {}).get("vnc"):
                data["vnc"] = connection["vnc"]
            instance = Instance(
                cid, "docker", str(self.root) if self.root else None, data, host=self.host
            )
            self.exec(instance, "true", None, 15)
            return instance
        except BaseException:
            try:
                docker("rm", "--force", identity)
            except Exception:
                pass
            raise

    def connect(self, record):
        host = _validate_host(record.get("host"))
        ssh = normalize_ssh(self.ssh or record.get("host_ssh"))
        cid = record.get("container_id")
        if not isinstance(cid, str) or not cid or cid.startswith("-"):
            raise ValueError("container_id is required")
        info = json.loads(self._docker("inspect", cid, host=host, ssh=ssh))[0]
        if not info["State"]["Running"]:
            raise RuntimeError("Container is not running")
        config = {}
        if record.get("python"):
            config["python"] = record["python"]
        data = {
            "ref": RuntimeRef("docker", info["Image"], config).to_dict(),
            "host_ssh": ssh,
            "python": config.get("python", "python3"),
        }
        if record.get("vnc"):
            data["vnc"] = record["vnc"]
        instance = Instance(info["Id"], "docker", str(self.root) if self.root else None, data, host)
        self.exec(instance, "true", timeout=15)
        return instance

    def describe(self, instance):
        record = {"container_id": instance.id}
        python = instance.data.get("ref", {}).get("config", {}).get("python")
        if python and python != "python3":
            record["python"] = python
        if instance.data.get("vnc"):
            record["vnc"] = instance.data["vnc"]
        return record

    def screenshot(self, instance):
        from ._vnc import screenshot

        return screenshot(instance.data.get("vnc"))

    def exec(
        self,
        instance: Instance,
        command: str | list[str],
        cwd: str | None = None,
        timeout: float | None = 300,
    ) -> tuple[int, str, str]:
        source = Path(__file__).with_name("_command.py").read_text()
        python = instance.data["ref"]["config"].get("python", "python3")
        raw = self._docker(
            "exec",
            instance.id,
            python,
            "-c",
            source,
            json.dumps({"command": command, "cwd": cwd, "timeout": timeout}),
            timeout=None if timeout is None else timeout + 15,
            host=instance.host,
            ssh=instance.data.get("host_ssh", self.ssh),
        )
        result = json.loads(raw)
        if "error" in result:
            raise TimeoutError(result["message"])
        return result["exit_code"], result["stdout"], result["stderr"]

    def upload(self, instance: Instance, source: str | Path, target: str) -> None:
        docker = partial(
            self._docker, host=instance.host, ssh=instance.data.get("host_ssh", self.ssh)
        )
        local = Path(source).resolve(strict=True)
        parent = posixpath.dirname(target) or "."
        code, _, error = self.exec(instance, f"mkdir -p -- {shlex.quote(parent)}")
        if code:
            raise RuntimeError(error)
        if local.is_dir():
            code, _, error = self.exec(instance, f"mkdir -p -- {shlex.quote(target)}")
            if code:
                raise RuntimeError(error)
            docker("cp", str(local) + "/.", f"{instance.id}:{target}")
        else:
            code, _, _ = self.exec(instance, f"test -d {shlex.quote(target)}")
            if code == 0:
                raise IsADirectoryError(target)
            docker("cp", str(local), f"{instance.id}:{target}")
        # docker cp creates root-owned files even when the image runs as a desktop user.
        # Give the copied working tree to that user so the solving Agent can edit it.
        python = instance.data["ref"]["config"].get("python", "python3")
        owner = docker(
            "exec",
            instance.id,
            python,
            "-c",
            "import os; print(str(os.getuid()) + ':' + str(os.getgid()))",
        )
        docker("exec", "--user", "0", instance.id, "chown", "-Rh", owner, "--", target)

    def download(self, instance: Instance, source: str, target: str | Path) -> None:
        docker = partial(
            self._docker, host=instance.host, ssh=instance.data.get("host_ssh", self.ssh)
        )
        destination = Path(target).absolute()
        destination.parent.mkdir(parents=True, exist_ok=True)
        code, _, _ = self.exec(instance, f"test -d {shlex.quote(source)}")
        if code == 0:
            destination.mkdir(parents=True, exist_ok=True)
            docker("cp", f"{instance.id}:{source}/.", destination)
        else:
            if destination.is_dir():
                raise IsADirectoryError(destination)
            docker("cp", f"{instance.id}:{source}", destination)

    def save(self, instance: Instance) -> RuntimeRef:
        docker = partial(
            self._docker, host=instance.host, ssh=instance.data.get("host_ssh", self.ssh)
        )
        identity = uuid.uuid4().hex
        tag = f"envweaver/snapshot:{identity}"
        config = dict(instance.data["ref"]["config"])
        # Flush filesystem buffers before the paused commit; application saves belong to the caller.
        code, _, error = self.exec(instance, "sync")
        if code:
            raise RuntimeError(error)
        # Commit pauses by default. Read the ID separately from CLI progress/warnings.
        docker("commit", instance.id, tag, timeout=600)
        image_id = docker("image", "inspect", "--format", "{{.Id}}", tag)
        connection = fixed_connection("docker", {"vnc": instance.data.get("vnc", {})}, strict=False)
        ref = RuntimeRef("docker", image_id, config, connection)
        records = {
            "runtime.json": ref.to_dict(),
            "metadata.json": {"tag": tag, "host": instance.host},
        }
        if self.root is not None:
            write_records(self.root, f"runtimes/{identity}", records, ssh_options=self.ssh)
        return ref

    def close(self, instance: Instance) -> None:
        docker = partial(
            self._docker, host=instance.host, ssh=instance.data.get("host_ssh", self.ssh)
        )
        docker("rm", "--force", instance.id)
