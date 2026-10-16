import os
from pathlib import Path

from .._util import read_json, write_json
from ..models import Instance, RuntimeRef
from .base import RuntimeBackend


class Runtime:
    def __init__(self, root=None, *, backends=None):
        self.root = (
            Path(root or os.environ.get("ENVWEAVER_HOME", "~/.envweaver")).expanduser().resolve()
        )
        self._backends = dict(backends or {})

    def register(self, name: str, backend: RuntimeBackend):
        self._backends[name] = backend

    def _backend(self, name):
        if name not in self._backends:
            if name == "docker":
                from .docker import DockerBackend

                self._backends[name] = DockerBackend(self.root)
            elif name == "qemu":
                from .qemu import QemuBackend

                self._backends[name] = QemuBackend(self.root)
            else:
                raise ValueError(f"Unknown runtime backend: {name}")
        return self._backends[name]

    def start(self, ref, context=None):
        if isinstance(ref, (str, Path)):
            ref = RuntimeRef.from_dict(read_json(ref))
        backend = self._backend(ref.backend)
        instance = backend.start(ref)
        try:
            for item in context or []:
                backend.upload(instance, item["source"], item["target"])
        except BaseException:
            backend.close(instance)
            raise
        return instance

    def exec(self, instance, command, cwd=None, timeout=300):
        return self._backend(instance.backend).exec(instance, command, cwd, timeout)

    def upload(self, instance, local_path, remote_path):
        return self._backend(instance.backend).upload(instance, str(local_path), remote_path)

    def download(self, instance, remote_path, local_path):
        return self._backend(instance.backend).download(instance, remote_path, str(local_path))

    def save(self, instance, name):
        return self._backend(instance.backend).save(instance, name)

    def close(self, instance):
        try:
            self._backend(instance.backend).close(instance)
        except Exception as error:
            return False, str(error)
        return True, None

    @staticmethod
    def connect(path):
        return Instance.from_dict(read_json(path))

    @staticmethod
    def describe(instance, path):
        write_json(path, instance.to_dict(), private=True)


def start(ref, context=None):
    return Runtime().start(ref, context)


def exec(instance, command, cwd=None, timeout=300):
    return Runtime(instance.root).exec(instance, command, cwd, timeout)


def upload(instance, local_path, remote_path):
    return Runtime(instance.root).upload(instance, local_path, remote_path)


def download(instance, remote_path, local_path):
    return Runtime(instance.root).download(instance, remote_path, local_path)


def save(instance, name):
    return Runtime(instance.root).save(instance, name)


def close(instance):
    return Runtime(instance.root).close(instance)


connect = Runtime.connect
