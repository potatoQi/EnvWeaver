class EnvWeaverError(Exception):
    """Base error for infrastructure failures (never a task score)."""


class RuntimeError(EnvWeaverError):
    pass


class VerifierError(EnvWeaverError):
    pass


class AgentError(EnvWeaverError):
    pass


class TaskExistsError(EnvWeaverError):
    pass
