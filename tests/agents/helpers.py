"""Cooperative fake Agent for lifecycle and evaluation tests."""

import threading

from envweaver import Agent


class CooperativeAgent(Agent):
    def __init__(self, **config):
        super().__init__(**config)
        self._cancelled = threading.Event()

    def stop(self, timeout=30):
        self._cancelled.set()

    def _check_stopped(self):
        if self._cancelled.is_set():
            raise InterruptedError("Agent stopped")
