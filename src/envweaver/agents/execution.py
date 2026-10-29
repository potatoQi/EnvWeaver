"""Managed execution of an Agent, independent of adapter implementation."""

import sys
import threading
import time
import weakref
from copy import deepcopy
from pathlib import Path

from .._io.paths import normalize_address
from .._io.storage import local_source
from .._util import positive, run_python, validate_cwd, validate_goal
from ..models import Instance
from ..runtime import Runtime
from .output import AgentOutput, validate_result

_claimed = weakref.WeakSet()
_claim_lock = threading.Lock()


class AgentExecution:
    def __init__(self, agent):
        with _claim_lock:
            if agent in _claimed:
                raise ValueError("Use a fresh Agent for each evaluation")
            _claimed.add(agent)
        self.agent = agent
        self.output = None
        self._lock = threading.RLock()
        self._cancelled = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._state = "new"
        self._runtime = None
        self._instance = None
        self._output_address = None
        self._cwd = None

    def preflight(self, goal):
        validate_goal(goal)
        self.agent.preflight(goal=goal)

    def _check_stopped(self):
        if self._cancelled.is_set():
            raise InterruptedError("Agent stopped")

    def cancel(self):
        """Block further stages; stop() waits for any active adapter call to finish."""
        self._cancelled.set()

    def prepare(
        self, runtime: Runtime, instance: Instance, output_dir: str | Path,
        *, cwd: str,
    ) -> None:
        validate_cwd(cwd)
        with self._lock:
            if self._state != "new":
                raise ValueError("Each Agent can only be prepared once")
            self._check_stopped()
            self._runtime, self._instance = runtime, instance
            self._cwd = cwd
            self._output_address = normalize_address(output_dir)
            if isinstance(self._output_address, Path):
                self._output_address = self._output_address.resolve()
            self._state = "preparing"
            self._idle.clear()
        try:
            self.output = AgentOutput(self._output_address, self.agent.metadata)
            self.output.write_log("prepare.log", "")
            self._check_stopped()
            code, _, stderr = run_python(
                runtime=runtime, instance=instance,
                source=f"import os; os.chdir({self._cwd!r})", timeout=30,
            )
            if code:
                raise RuntimeError(f"Cannot enter Agent working directory {self._cwd!r}: {stderr}")
            self._check_stopped()
            self.agent.prepare(runtime=runtime, instance=instance,
                               output=self.output, cwd=cwd)
            self._check_stopped()
        except BaseException:
            with self._lock:
                self._state = "failed"
            raise
        finally:
            original_error = sys.exception()
            try:
                if self.output is not None:
                    self.output.sync()
            except Exception as error:
                self._state = "failed"
                if original_error is None:
                    raise
                original_error.add_note(f"Final Agent output synchronization failed: {error}")
                for note in getattr(error, "__notes__", []):
                    original_error.add_note(note)
            finally:
                with self._lock:
                    if self._state == "preparing":
                        self._state = "ready"
                    self._idle.set()

    def run(
        self, goal: list[dict], runtime: Runtime, instance: Instance, output_dir: str | Path
    ) -> dict:
        address = normalize_address(output_dir)
        if isinstance(address, Path):
            address = address.resolve()
        with self._lock:
            if self._state != "ready":
                raise ValueError("run requires a prepared, unused Agent")
            if (
                runtime is not self._runtime
                or instance != self._instance
                or address != self._output_address
            ):
                raise ValueError(
                    "run must use the same runtime, instance and output_dir as prepare"
                )
            self._state = "running"
            self._idle.clear()
        started = time.monotonic()
        try:
            self._check_stopped()
            self.preflight(goal=goal)
            recorded_goal = deepcopy(goal)
            prepared_goal = deepcopy(goal)
            for index, block in enumerate(recorded_goal):
                if block["type"] == "image":
                    with local_source(block["path"]) as source:
                        block["path"] = self.output.save_file(source.read_bytes(), source.suffix)
                    prepared_goal[index]["path"] = str(self.output.directory / block["path"])
                    self._check_stopped()
            self.output.record("input", {"goal": recorded_goal})
            result = self.agent.run(goal=prepared_goal, runtime=runtime, instance=instance,
                                    output=self.output, cwd=self._cwd)
            self._check_stopped()
            validate_result(result, self.output.directory)
            self.output.update(**result)
        except BaseException:
            with self._lock:
                self._state = "failed"
            raise
        finally:
            original_error = sys.exception()
            try:
                self.output.update(metadata={"duration_seconds": time.monotonic() - started})
            except Exception as error:
                with self._lock:
                    self._state = "failed"
                if original_error is None:
                    raise
                original_error.add_note(f"Final Agent snapshot failed: {error}")
                for note in getattr(error, "__notes__", []):
                    original_error.add_note(note)
            finally:
                with self._lock:
                    if self._state == "running":
                        self._state = "finished"
                    self._idle.set()
        self._check_stopped()
        with self._lock:
            return self.output.result

    def stop(self, timeout: float = 30) -> None:
        positive(timeout, "timeout")
        with self._lock:
            if self._state in ("new", "finished", "stopped"):
                return
            self.cancel()
        deadline = time.monotonic() + timeout
        # The adapter's stop hook itself may block on a transport. Bound that too.
        done = threading.Event()
        errors = []

        def request():
            try:
                self.agent.stop(timeout=max(0, deadline - time.monotonic()))
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()

        threading.Thread(target=request, daemon=True, name="envweaver-agent-stop").start()
        if not done.wait(max(0, deadline - time.monotonic())):
            raise TimeoutError("Agent stop exceeded timeout")
        if errors:
            raise errors[0]
        if not self._idle.wait(max(0, deadline - time.monotonic())):
            raise TimeoutError("Agent is still executing after stop")
        with self._lock:
            self._state = "stopped"
