from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class RuntimeRef:
    backend: str
    image: str
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


@dataclass(frozen=True)
class Instance:
    id: str
    backend: str
    root: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


@dataclass(frozen=True)
class Task:
    name: str
    task_ref: str
    runtime: RuntimeRef
    goal: list[dict[str, Any]]
    verifier: str
    context: list[dict[str, str]]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class AgentResult:
    text: str
    workspace: str
    trajectory: str
    session: str | None = None
