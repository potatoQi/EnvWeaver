import threading
from functools import partial

from .manager import Runtime


class _AttemptRuntime:
    """Delegate runtime operations and own instances started during one Eval attempt."""

    def __init__(self, runtime, cleanup):
        self._runtime = runtime
        self._cleanup = cleanup
        self._lock = threading.Lock()
        self._cancelled = False
        self._instances = {}

    @property
    def __class__(self):
        # Preserve isinstance(runtime, Runtime) and custom runtime type checks in agents.
        return type(self._runtime)

    def __getattr__(self, name):
        return getattr(self._runtime, name)

    def _check(self):
        with self._lock:
            if self._cancelled:
                raise InterruptedError("Evaluation resources are closed")

    def _cancel(self):
        with self._lock:
            self._cancelled = True

    def start(self, *args, **kwargs):
        self._check()
        if getattr(self._runtime.start, "__func__", None) is Runtime.start:
            return self._runtime._start(
                *args, **kwargs, on_started=self._adopt, check_cancelled=self._check,
            )
        instance = self._runtime.start(*args, **kwargs)
        self._adopt(instance)
        return instance

    @staticmethod
    def _key(instance):
        return instance.backend, instance.host, instance.id

    def _adopt(self, instance):
        entry = (instance, threading.Lock())
        with self._lock:
            self._instances[self._key(instance)] = entry
            late = self._cancelled
        if late:
            self._cleanup(partial(self._release, entry))
            raise InterruptedError("Sandbox started after evaluation stopped")

    def close(self, instance):
        with self._lock:
            entry = self._instances.get(self._key(instance))
            if entry is None and self._cancelled:
                raise InterruptedError("Evaluation resources are closed")
        if entry is None:
            return self._runtime.close(instance=instance)
        self._release(entry)

    def _release(self, entry):
        # An explicit close and final cleanup can race. A failed close stays registered.
        instance, lock = entry
        key = self._key(instance)
        with lock:
            with self._lock:
                if self._instances.get(key) is not entry:
                    return
            self._runtime.close(instance=instance)
            with self._lock:
                if self._instances.get(key) is entry:
                    del self._instances[key]

    def _finish(self):
        with self._lock:
            entries = list(reversed(self._instances.values()))
        for entry in entries:
            self._cleanup(partial(self._release, entry))
