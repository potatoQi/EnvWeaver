"""Per-call OpenSSH configuration; never edits the user's SSH files."""

import os
import shlex
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .._util import merge_dicts

_FILES = ("config_file", "known_hosts_file", "identity_file")


def reconnect_options(saved, overrides):
    """None inherits a record; {} resets it; nonempty overrides merge recursively."""
    if overrides is None:
        return normalize(saved)
    if not overrides:
        return {}

    return normalize(merge_dicts(saved or {}, overrides))


def normalize(options):
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise TypeError("SSH configuration must be a dictionary")
    result = dict(options)
    for key in (*_FILES, "host_key_alias"):
        if key not in result:
            continue
        value = os.fspath(result[key]) if isinstance(result[key], Path) else result[key]
        if not isinstance(value, str) or not value.strip() or any(c in value for c in "\x00\r\n"):
            raise ValueError(f"Invalid ssh.{key}")
        if key == "host_key_alias":
            if any(c.isspace() for c in value):
                raise ValueError("Invalid ssh.host_key_alias")
            result[key] = value
        else:
            result[key] = str(Path(value).expanduser().absolute())
    return result


def flags(options):
    options = normalize(options)
    args = []
    if "config_file" in options:
        args += ["-F", options["config_file"]]
    if "known_hosts_file" in options:
        # OpenSSH parses this option as a list of filenames, even in one argv item.
        path = (
            options["known_hosts_file"].replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
        )
        args += ["-o", f'UserKnownHostsFile="{path}"']
    if "identity_file" in options:
        if not Path(options["identity_file"]).is_file():
            raise FileNotFoundError(options["identity_file"])
        args += ["-i", options["identity_file"]]
    if "host_key_alias" in options:
        args += ["-o", "HostKeyAlias=" + options["host_key_alias"]]
    return args


@contextmanager
def docker_environment(env, options):
    """Docker invokes ssh via PATH; supply options only to this Docker subprocess."""
    args = flags(options)
    if not args:
        yield env
        return
    executable = shutil.which("ssh", path=env.get("PATH"))
    if executable is None:
        raise FileNotFoundError("ssh executable not found")
    with tempfile.TemporaryDirectory(prefix="envweaver-ssh-") as directory:
        wrapper = Path(directory) / "ssh"
        wrapper.write_text("#!/bin/sh\nexec " + shlex.join([executable, *args]) + ' "$@"\n')
        wrapper.chmod(0o700)
        yield {**env, "PATH": directory + os.pathsep + env.get("PATH", os.defpath)}
