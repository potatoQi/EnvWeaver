from typing import Protocol

from ..models import AgentResult


class AgentBackend(Protocol):
    def run(
        self, instruction, workspace, tools=None, instance=None, config=None
    ) -> AgentResult: ...


_backends = {}


def register(name: str, backend: AgentBackend):
    """Register an adapter; no pipeline or session policy is imposed."""
    _backends[name] = backend


def run(backend, instruction, workspace, tools=None, instance=None, config=None):
    if backend not in _backends:
        if backend == "pi":
            from .pi import PiBackend

            _backends[backend] = PiBackend()
        else:
            raise ValueError(f"Unknown Agent backend: {backend}")
    return _backends[backend].run(instruction, workspace, tools, instance, config)
