import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

from .._util import write_json
from ..errors import RuntimeError
from ..models import Instance, RuntimeRef
from ..transport import GuestClient


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
    """Local QEMU/KVM, qcow2 disks, and a prepared guest control service."""

    def __init__(self, root):
        self.root = Path(root)

    def _run(self, args):
        result = subprocess.run(args, capture_output=True, text=True, timeout=600)
        if result.returncode:
            raise RuntimeError(result.stderr)

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
            f"user,id=net0,hostfwd=tcp:127.0.0.1:{data['port']}-:{config.get('guest_port', 8765)}",
            "-device",
            f"{config.get('network_device', 'virtio-net-pci')},netdev=net0",
        ]
        if config.get("firmware"):
            args += ["-drive", f"if=pflash,format=raw,readonly=on,file={config['firmware']}"]
        if data.get("nvram"):
            args += ["-drive", f"if=pflash,format=raw,file={data['nvram']}"]
        args += config.get("args", [])
        self._run(args)
        deadline = time.monotonic() + config.get("startup_timeout", 180)
        while time.monotonic() < deadline:
            try:
                GuestClient(instance).health()
                return
            except Exception:
                time.sleep(0.25)
        raise RuntimeError("Guest control service did not become ready; check base image and token")

    def start(self, ref):
        import shutil

        directory = self.root / "instances" / uuid.uuid4().hex
        directory.mkdir(parents=True)
        # A short QMP socket path avoids AF_UNIX limits on long home paths.
        if len(str(directory / "qmp.sock").encode()) > 100:
            raise ValueError("QEMU Runtime root is too long; use a shorter local storage path")
        config = dict(ref.config)
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
        }
        if any(arg.split("=", 1)[0] in unmanaged for arg in config.get("args", [])):
            raise ValueError(
                "QEMU args cannot override managed storage, lifecycle, or QMP settings"
            )
        token_env = config.get("token_env", "ENVWEAVER_GUEST_TOKEN")
        if not os.environ.get(token_env):
            raise ValueError(f"Set {token_env} to the prepared guest service token")
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
                str(Path(ref.image).resolve(strict=True)),
                str(disk),
            ]
        )
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        data = {
            "ref": ref.to_dict(),
            "disk": str(disk),
            "port": port,
            "qmp": str(directory / "qmp.sock"),
            "pidfile": str(directory / "qemu.pid"),
            "guest_url": f"http://127.0.0.1:{port}",
            "token_env": token_env,
        }
        if config.get("nvram"):
            data["nvram"] = str(directory / "nvram.fd")
            shutil.copy2(config["nvram"], data["nvram"])
        instance = Instance(directory.name, "qemu", str(self.root), data)
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
        write_json(directory / "instance.json", instance.to_dict(), private=True)
        return instance

    def exec(self, instance, command, cwd=None, timeout=300):
        return GuestClient(instance).exec(command, cwd, timeout)

    def upload(self, instance, local_path, remote_path):
        return GuestClient(instance).upload(local_path, remote_path)

    def download(self, instance, remote_path, local_path):
        return GuestClient(instance).download(remote_path, local_path)

    def _shutdown(self, instance):
        config = instance.data["ref"]["config"]
        with QMP(instance.data["qmp"]) as qmp:
            qmp.execute("system_powerdown")
        deadline = time.monotonic() + config.get("shutdown_timeout", 120)
        while time.monotonic() < deadline:
            # QEMU removes its PID file on exit. Never reuse a PID to kill an unrelated process.
            if not Path(instance.data["pidfile"]).exists():
                return
            time.sleep(0.25)
        raise RuntimeError("Guest did not shut down cleanly; snapshot was not created")

    def save(self, instance, name):
        import shutil

        config = dict(instance.data["ref"]["config"])
        asset = self.root / "runtimes" / uuid.uuid4().hex
        asset.mkdir(parents=True)
        self._shutdown(instance)
        try:
            self._run(
                [
                    config.get("image_binary", "qemu-img"),
                    "convert",
                    "-O",
                    "qcow2",
                    instance.data["disk"],
                    str(asset / "disk.qcow2"),
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
            ref = RuntimeRef("qemu", str(asset / "disk.qcow2"), config)
            write_json(asset / "runtime.json", ref.to_dict())
            write_json(asset / "metadata.json", {"name": name})
        finally:
            self._launch(instance)
        return ref

    def close(self, instance):
        import shutil

        with QMP(instance.data["qmp"]) as qmp:
            qmp.execute("quit")
        deadline = time.monotonic() + 15
        while Path(instance.data["pidfile"]).exists():
            if time.monotonic() > deadline:
                raise RuntimeError("QEMU did not exit; live files were preserved")
            time.sleep(0.1)
        shutil.rmtree(Path(instance.data["disk"]).parent)
