"""Load only patches verified against the installed Harbor version."""

from importlib.metadata import version


def install() -> None:
    """Install process-local patches; reject unverified Harbor versions."""
    installed = version("harbor")
    if installed != "0.25.0":
        raise ValueError(f"No verified Terminus monkey patches for Harbor {installed}")

    from .v0_25_0.context_recovery import install as install_context_recovery

    install_context_recovery()
