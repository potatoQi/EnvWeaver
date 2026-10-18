import json
import shutil
import socket
import subprocess
import time
import uuid
from dataclasses import replace
from pathlib import Path, PurePosixPath

from .._paths import normalize_address
from .._ssh_config import normalize as normalize_ssh
from .._ssh_transport import _validate_host
from .._storage import remove_records, write_records
from ..errors import RuntimeError
from ..models import Instance, RuntimeRef
from . import _remote, _ssh
from ._connection import fixed_connection, merge_connection


class QMP:
    def __init__(self, path, timeout=10):
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.settimeout(timeout)
        try:
            self.socket.connect(str(path))
            self.stream = self.socket.makefile("rwb")
            greeting = json.loads(self.stream.readline())
            if "QMP" not in greeting:
                raise RuntimeError("Invalid QMP greeting")
            self.execute("qmp_capabilities")
        except BaseException:
            if hasattr(self, "stream"):
                self.stream.close()
            self.socket.close()
            raise

    def execute(self, command, arguments=None):
        identity = uuid.uuid4().hex
        self.stream.write(
            json.dumps({"execute": command, "arguments": arguments or {}, "id": identity}).encode()
            + b"\n"
        )
        self.stream.flush()
        while line := self.stream.readline():
            result = json.loads(line)
            if result.get("id") == identity:
                if "error" in result:
                    raise RuntimeError(str(result["error"]))
                return result.get("return")
        raise RuntimeError("QMP connection closed")

    def close(self):
        self.stream.close()
        self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class QemuBackend:
    """Local or SSH-hosted QEMU/KVM with qcow2 disks and a OpenSSH guest connection."""

    def __init__(
        self, root: str | Path | None = None, *, host: str | None = None, ssh: dict | None = None
    ):
        self.root = normalize_address(root) if root is not None else None
        self.host = _validate_host(host)
        self.ssh = normalize_ssh(ssh)
        self._check_ssh = True

    def _call_remote(self, action, host, config, *, root, **payload):
        payload.update(action=action, root=str(root) if root is not None else None)
        timeout = (
            config.get("startup_timeout", 180) + config.get("shutdown_timeout", 120) + 630
            if action != "close"
            else 45
        )
        options = payload.get("instance", {}).get("data", {}).get("host_ssh", self.ssh)
        if action == "connect":
            options = self.ssh or payload["record"].get("host_ssh", {})
        return _remote.call(
            host,
            config.get("remote_python", "python3"),
            payload,
            timeout=timeout,
            **({"ssh_options": options} if options else {}),
        )

    def _run(self, args):
        result = subprocess.run(args, capture_output=True, text=True, timeout=600)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout

    def _launch(self, instance):
        data = instance.data
        config = data["ref"]["config"]
        Path(data["qmp"]).unlink(missing_ok=True)
        args = [
            config.get("binary", "qemu-system-x86_64"),
            "-daemonize",
            "-pidfile",
            data["pidfile"],
            "-name",
            instance.id,
            "-machine",
            config.get("machine", "q35"),
            "-accel",
            config.get("accel", "kvm"),
            "-m",
            str(config.get("memory", 4096)),
            "-smp",
            str(config.get("cpus", 2)),
            "-drive",
            f"file={data['disk']},format=qcow2,if={config.get('disk_interface', 'virtio')}",
            "-qmp",
            f"unix:{data['qmp']},server=on,wait=off",
            "-display",
            "none",
            "-netdev",
            f"user,id=net0,hostfwd=tcp:127.0.0.1:{data['port']}-:22",
            "-device",
            f"{config.get('network_device', 'virtio-net-pci')},netdev=net0",
        ]
        if config.get("firmware"):
            args += ["-drive", f"if=pflash,format=raw,readonly=on,file={config['firmware']}"]
        if data.get("nvram"):
            args += ["-drive", f"if=pflash,format=raw,file={data['nvram']}"]
        args += config.get("args", [])
        self._run(args)
        if self._check_ssh:
            self._wait_ready(instance)

    @staticmethod
    def _wait_ready(instance):
        data = instance.data
        config = data["ref"]["config"]
        deadline = time.monotonic() + config.get("startup_timeout", 180)
        while True:
            try:
                _ssh.request(data["ssh"], "health", timeout=10)
                break
            except (RuntimeError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("VM SSH service did not become ready")
                time.sleep(0.25)

    @staticmethod
    def _disk_dir(config):
        value = config.get("disk_dir")
        if (
            not isinstance(value, str)
            or not value.strip()
            or not PurePosixPath(value).is_absolute()
        ):
            raise ValueError("QEMU config.disk_dir is required and must be an absolute directory")
        return Path(value)

    @staticmethod
    def _save_mode(config):
        mode = config.get("save_mode", "incremental")
        if mode not in ("incremental", "consolidated"):
            raise ValueError(f"Invalid QEMU config.save_mode: {mode}")
        return mode

    def create(
        self, image: str, config: dict | None = None, connection: dict | None = None
    ) -> RuntimeRef:
        connection = fixed_connection("qemu", connection)
        config = dict(config or {})
        self._save_mode(config)
        self._disk_dir(config)
        if self.host is not None:
            self._check_remote_paths(image, config)
            result = self._call_remote(
                "create",
                self.host,
                config,
                root=self.root,
                ref=RuntimeRef("qemu", image, config, connection).to_dict(),
            )
            return RuntimeRef.from_dict(result)
        image = str(Path(image).resolve(strict=True))
        with open(image, "rb"):
            pass
        for key in ("firmware", "nvram"):
            if config.get(key):
                config[key] = str(Path(config[key]).resolve(strict=True))
                with open(config[key], "rb"):
                    pass
        return RuntimeRef("qemu", image, config, connection)

    @staticmethod
    def _check_remote_paths(image, config):
        for value in (image, config.get("firmware"), config.get("nvram")):
            if value and not PurePosixPath(value).is_absolute():
                raise ValueError(
                    "Remote QEMU image, firmware and nvram must use absolute host paths"
                )

    def start(self, ref: RuntimeRef, connection: dict | None = None) -> Instance:
        self._save_mode(ref.config)
        self._disk_dir(ref.config)
        connection = merge_connection(ref.connection, connection)
        connection["ssh"] = normalize_ssh(connection.get("ssh"))
        if self.host is not None:
            self._check_remote_paths(ref.image, ref.config)
            result = self._call_remote(
                "start",
                self.host,
                ref.config,
                root=self.root,
                ref=ref.to_dict(),
                connection=connection,
            )
            instance = replace(Instance.from_dict(result), host=self.host)
            instance.data["ssh"]["via"] = self.host
            if self.ssh:
                instance.data["ssh"]["via_ssh"] = self.ssh
        else:
            instance = self._start(ref, connection)
        instance.data["host_ssh"] = self.ssh
        try:
            if self.host is not None:
                self._wait_ready(instance)
            if self.root is None:
                return instance
            write_records(
                self.root,
                f"instances/{instance.id}",
                {
                    "instance.json": {
                        "version": 1,
                        "backend": "qemu",
                        "host": instance.host,
                        **({"host_ssh": self.ssh} if self.ssh else {}),
                        **self.describe(instance),
                    }
                },
                private=True,
                ssh_options=self.ssh,
            )
        except BaseException as error:
            try:
                self.close(instance)
            except Exception as cleanup_error:
                error.add_note(f"Instance cleanup also failed: {cleanup_error}")
            raise
        return instance

    @staticmethod
    def _validate_args(config):
        unmanaged = {
            "-drive",
            "-blockdev",
            "-hda",
            "-hdb",
            "-hdc",
            "-hdd",
            "-cdrom",
            "-pflash",
            "-snapshot",
            "-loadvm",
            "-incoming",
            "-qmp",
            "-pidfile",
            "-daemonize",
            "-netdev",
            "-nic",
            "-net",
            "-smp",
            "-m",
            "-machine",
            "-accel",
            "-name",
        }
        if any(arg.split("=", 1)[0] in unmanaged for arg in config.get("args", [])):
            raise ValueError(
                "QEMU args cannot override managed storage, lifecycle, or QMP settings"
            )

    def _start(self, ref: RuntimeRef, connection: dict | None = None) -> Instance:
        # Pin the backing path before the caller can change its working directory.
        ref = replace(ref, image=str(Path(ref.image).resolve(strict=True)))
        directory = self._disk_dir(ref.config) / "instances" / uuid.uuid4().hex
        # A short QMP socket path avoids AF_UNIX limits on long home paths.
        if len(str(directory / "qmp.sock").encode()) > 100:
            raise ValueError("QEMU disk_dir is too long; use a shorter path")
        config = dict(ref.config)
        self._validate_args(config)
        ssh = dict((connection or {}).get("ssh", {}))
        if not ssh.get("user"):
            raise ValueError("connection.ssh.user is required")
        if set(config) & {"guest_port", "token_env"}:
            raise ValueError("Use connection.ssh instead of guest_port/token_env")
        directory.mkdir(parents=True)
        (directory / ".envweaver-owned").write_text(directory.name)
        disk = directory / "disk.qcow2"
        self._run(
            [
                config.get("image_binary", "qemu-img"),
                "create",
                "-f",
                "qcow2",
                "-F",
                config.get("format", "qcow2"),
                "-b",
                ref.image,
                str(disk),
            ]
        )
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        data = {
            "python": ssh.get("python", "python3"),
            "ref": ref.to_dict(),
            "disk": str(disk),
            "port": port,
            "qmp": str(directory / "qmp.sock"),
            "pidfile": str(directory / "qemu.pid"),
            "ssh": {**ssh, "host": "127.0.0.1", "port": port},
            "owned_directory": str(directory),
        }
        if config.get("nvram"):
            data["nvram"] = str(directory / "nvram.fd")
            shutil.copy2(config["nvram"], data["nvram"])
        instance = Instance(directory.name, "qemu", str(self.root) if self.root else None, data)
        try:
            self._launch(instance)
        except BaseException:
            if Path(data["qmp"]).exists():
                try:
                    with QMP(data["qmp"]) as qmp:
                        qmp.execute("quit")
                except Exception:
                    pass
            raise
        return instance

    def describe(self, instance):
        config = dict(instance.data["ref"]["config"])
        for key in ("disk_dir", "memory", "cpus", "format"):
            config.pop(key, None)
        if instance.data.get("nvram"):
            config["nvram"] = instance.data["nvram"]
        for key, value in {
            "machine": "q35",
            "accel": "kvm",
            "disk_interface": "virtio",
            "network_device": "virtio-net-pci",
            "args": [],
        }.items():
            config.setdefault(key, value)
        ssh = dict(instance.data["ssh"])
        if ssh.get("via") == instance.host:
            ssh.pop("via", None)
            ssh.pop("via_ssh", None)
        return {"qmp": instance.data["qmp"], "ssh": ssh, "startup": config}

    def connect(self, record):
        host = _validate_host(record.get("host"))
        host_ssh = normalize_ssh(self.ssh or record.get("host_ssh"))
        record = {**record, "host_ssh": host_ssh, "ssh": normalize_ssh(record.get("ssh"))}
        startup = record.get("startup")
        if not isinstance(startup, dict):
            raise ValueError("QEMU startup configuration is required")
        if host:
            result = self._call_remote("connect", host, startup, root=self.root, record=record)
            instance = replace(Instance.from_dict(result), host=host)
            instance.data["ssh"]["via"] = host
            if host_ssh:
                instance.data["ssh"]["via_ssh"] = host_ssh
            instance.data["host_ssh"] = host_ssh
            _ssh.request(instance.data["ssh"], "health", timeout=15)
            return instance
        instance = self._connect(record)
        instance.data["host_ssh"] = host_ssh
        return instance

    def _connect(self, record):
        qmp = record.get("qmp")
        if not isinstance(qmp, str) or not Path(qmp).is_absolute():
            raise ValueError("qmp must be an absolute socket path")
        config = dict(record["startup"])
        required = {"machine", "accel", "disk_interface", "network_device", "args"}
        if not required <= config.keys():
            raise ValueError(f"startup requires {sorted(required)}")
        ssh = dict(record.get("ssh", {}))
        if not ssh.get("user") or not ssh.get("host"):
            raise ValueError("ssh.host and ssh.user are required")
        if (
            ssh["host"] != "127.0.0.1"
            or not isinstance(ssh.get("port"), int)
            or not 1024 <= ssh["port"] <= 65535
        ):
            raise ValueError("QEMU connect requires localhost SSH forwarding on a port >= 1024")
        self._validate_args(config)
        # Supported layout: one writable qcow2 disk, optional pflash, native user networking.
        with QMP(qmp) as monitor:
            if monitor.execute("query-status")["status"] != "running":
                raise RuntimeError("QEMU instance is not running")
            blocks = monitor.execute("query-block")
            disks = [
                item["inserted"]
                for item in blocks
                if item.get("inserted")
                and not item.get("removable")
                and item["inserted"].get("drv") == "qcow2"
            ]
            writable = [
                item
                for item in blocks
                if item.get("inserted")
                and not item.get("removable")
                and not item["inserted"].get("ro", False)
            ]
            if len(disks) != 1 or disks[0].get("ro"):
                raise ValueError("connect supports one writable qcow2 system disk")
            if len(writable) > (2 if config.get("nvram") else 1):
                raise ValueError("Additional writable disks are not supported")
            allowed = {disks[0]["file"], config.get("firmware"), config.get("nvram")}
            if any(
                item["inserted"]["file"] not in allowed for item in blocks if item.get("inserted")
            ):
                raise ValueError("Additional disks or inserted media are not supported")
            disk = disks[0]["file"]
            cpus = monitor.execute("query-cpus-fast")
            memory = monitor.execute("query-memory-size-summary")
            identity = monitor.execute("query-name").get("name") or Path(qmp).parent.name
        if memory.get("plugged-memory", 0):
            raise ValueError("Hotplug memory is not supported")
        config.update(cpus=len(cpus), memory=memory["base-memory"] // (1024 * 1024))
        data = {
            "python": ssh.get("python", "python3"),
            "ref": RuntimeRef("qemu", disk, config).to_dict(),
            "disk": disk,
            "qmp": qmp,
            "ssh": ssh,
            "port": ssh["port"],
        }
        directory = Path(qmp).parent
        marker = directory / ".envweaver-owned"
        if (
            marker.is_file()
            and marker.read_text() == identity
            and directory.name == identity
            and Path(disk) == directory / "disk.qcow2"
        ):
            data["owned_directory"] = str(directory)
            data["pidfile"] = str(directory / "qemu.pid")
        if config.get("nvram"):
            data["nvram"] = config["nvram"]
        if self._check_ssh:
            _ssh.request(ssh, "health", timeout=15)
        return Instance(identity, "qemu", str(self.root) if self.root else None, data)

    def screenshot(self, instance):
        import base64
        import tempfile

        if instance.host:
            result = self._call_remote(
                "screenshot", instance.host, {}, root=None, instance=instance.to_dict()
            )
            return base64.b64decode(result)
        with tempfile.TemporaryDirectory(prefix="envweaver-screen-") as directory:
            path = Path(directory) / "screen.png"
            with QMP(instance.data["qmp"]) as monitor:
                monitor.execute("screendump", {"filename": str(path), "format": "png"})
            return path.read_bytes()

    def exec(
        self,
        instance: Instance,
        command: str | list[str],
        cwd: str | None = None,
        timeout: float | None = 300,
    ) -> tuple[int, str, str]:
        return _ssh.execute(instance.data["ssh"], command, cwd, timeout)

    def upload(self, instance: Instance, source: str | Path, target: str) -> None:
        return _ssh.upload(instance.data["ssh"], source, target)

    def download(self, instance: Instance, source: str, target: str | Path) -> None:
        return _ssh.download(instance.data["ssh"], source, target)

    @staticmethod
    def _running(instance):
        if instance.data.get("pidfile"):
            return Path(instance.data["pidfile"]).exists()
        try:
            with QMP(instance.data["qmp"], timeout=1) as monitor:
                monitor.execute("query-status")
            return True
        except (FileNotFoundError, ConnectionRefusedError):
            return False

    def _shutdown(self, instance):
        config = instance.data["ref"]["config"]
        with QMP(instance.data["qmp"]) as qmp:
            qmp.execute("system_powerdown")
        deadline = time.monotonic() + config.get("shutdown_timeout", 120)
        while time.monotonic() < deadline:
            # QEMU removes its PID file on exit. Never reuse a PID to kill an unrelated process.
            if not self._running(instance):
                return
            time.sleep(0.25)
        raise RuntimeError("Guest did not shut down cleanly; snapshot was not created")

    def save(self, instance: Instance) -> RuntimeRef:
        config = instance.data["ref"]["config"]
        self._save_mode(config)
        self._disk_dir(config)
        if instance.host is not None:
            result = self._call_remote(
                "save",
                instance.host,
                config,
                root=self.root,
                instance=instance.to_dict(),
            )
            ref = RuntimeRef.from_dict(result)
            self._wait_ready(instance)
        else:
            ref = self._save(instance)
        if self.root is not None:
            write_records(
                self.root,
                f"runtimes/{uuid.uuid4().hex}",
                {"runtime.json": ref.to_dict(), "metadata.json": {"host": instance.host}},
                ssh_options=self.ssh,
            )
        return ref

    def _save(self, instance: Instance) -> RuntimeRef:
        config = dict(instance.data["ref"]["config"])
        mode = self._save_mode(config)
        asset = self._disk_dir(config) / "runtimes" / uuid.uuid4().hex
        self._shutdown(instance)
        try:
            asset.mkdir(parents=True)
            # Preserve the original backing disk, never depend on the live instance's overlay.
            self._run(
                [
                    config.get("image_binary", "qemu-img"),
                    "convert",
                    "-f",
                    "qcow2",
                    "-O",
                    "qcow2",
                    "-B",
                    instance.data["ref"]["image"],
                    "-F",
                    config.get("format", "qcow2"),
                    instance.data["disk"],
                    str(asset / "disk.qcow2"),
                ]
            )
            if mode == "consolidated":
                image_binary = config.get("image_binary", "qemu-img")
                disk = str(asset / "disk.qcow2")
                chain = json.loads(
                    self._run([image_binary, "info", "--backing-chain", "--output=json", disk])
                )
                if len(chain) > 2:
                    # Safe rebase copies intermediate changes into the new snapshot only.
                    # Merely changing convert's backing file would lose those changes.
                    self._run(
                        [
                            image_binary,
                            "rebase",
                            "-f",
                            "qcow2",
                            "-b",
                            chain[-2]["full-backing-filename"],
                            "-F",
                            chain[-1]["format"],
                            disk,
                        ]
                    )
            config["format"] = "qcow2"
            for key, source in [
                ("firmware", config.get("firmware")),
                ("nvram", instance.data.get("nvram")),
            ]:
                if source:
                    target = asset / f"{key}.fd"
                    shutil.copy2(source, target)
                    config[key] = str(target)
            connection = fixed_connection(
                "qemu", {"ssh": instance.data.get("ssh", {})}, strict=False
            )
            ref = RuntimeRef("qemu", str(asset / "disk.qcow2"), config, connection)
        except BaseException:
            shutil.rmtree(asset, ignore_errors=True)
            raise
        finally:
            self._launch(instance)
        return ref

    def close(self, instance: Instance) -> None:
        if instance.host is not None:
            config = instance.data["ref"]["config"]
            self._call_remote(
                "close", instance.host, config, root=self.root, instance=instance.to_dict()
            )
        else:
            self._close(instance)
        if instance.root is not None:
            remove_records(
                instance.root,
                f"instances/{instance.id}",
                ssh_options=instance.data.get("host_ssh", self.ssh),
            )

    def _close(self, instance: Instance) -> None:
        with QMP(instance.data["qmp"]) as qmp:
            qmp.execute("quit")
        deadline = time.monotonic() + 15
        while self._running(instance):
            if time.monotonic() > deadline:
                raise RuntimeError("QEMU did not exit; live files were preserved")
            time.sleep(0.1)
        if instance.data.get("owned_directory"):
            shutil.rmtree(instance.data["owned_directory"])
