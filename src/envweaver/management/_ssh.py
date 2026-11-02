"""Management-only SSH connection reuse; user SSH configuration stays untouched."""

import hashlib
import json
import tempfile
from functools import lru_cache
from pathlib import Path
from threading import Lock

_config_lock = Lock()

# Cache only our executable code, never Pool contents. A short bootstrap checks
# the exact code digest on every call, so removal/corruption simply reinstalls it.
_CACHE = """
import hashlib, io, json, os, pathlib, sys, tempfile
cache = pathlib.Path(tempfile.gettempdir()) / ('envweaver-management-' + str(os.getuid()))
"""
_RUN = _CACHE + """
digest = sys.argv[1]
try:
    source = (cache / (digest + '.py')).read_bytes()
except OSError:
    source = b''
if hashlib.sha256(source).hexdigest() == digest:
    exec(compile(source, '<envweaver-management>', 'exec'), {'__name__': '__main__', '__package__': None})
else:
    print(json.dumps({'worker_missing': True}))
"""
_INSTALL = _CACHE + """
payload = json.load(sys.stdin)
source = payload.pop('_worker_source').encode()
digest = hashlib.sha256(source).hexdigest()
try:
    cache.mkdir(mode=0o700, exist_ok=True)
    info = cache.lstat()
    if cache.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError('Unsafe worker cache')
    with tempfile.TemporaryDirectory(dir=cache) as staging:
        path = pathlib.Path(staging) / 'worker.py'
        path.write_bytes(source)
        os.replace(path, cache / (digest + '.py'))
except OSError:
    pass
sys.stdin = io.StringIO(json.dumps(payload))
exec(compile(source, '<envweaver-management>', 'exec'), {'__name__': '__main__', '__package__': None})
"""


@lru_cache(maxsize=1)
def connection_config():
    # Keep the directory alive for the service lifetime. OpenSSH expires idle
    # connections itself; the private directory prevents socket impersonation.
    directory = tempfile.TemporaryDirectory(prefix="ew-manager-ssh-")
    config = Path(directory.name) / "config"
    includes = [Path.home() / ".ssh/config", Path("/etc/ssh/ssh_config")]
    config.write_text(
        "Host *\n"
        "    ControlMaster auto\n"
        "    ControlPersist 60\n"
        "    Compression yes\n"
        f"    ControlPath {directory.name}/%C\n"
        + "".join(f"Include {json.dumps(str(path))}\n" for path in includes if path.is_file())
    )
    return directory, str(config)


def connection_options():
    with _config_lock:
        return {"config_file": connection_config()[1]}


def call(send, host, source, payload):
    options = dict(timeout=300, ssh_options=connection_options())
    digest = hashlib.sha256(source.encode()).hexdigest()
    output = send(host, ["python3", "-c", _RUN, digest], payload, **options)
    result = json.loads(output)
    if result == {"worker_missing": True}:
        output = send(
            host, ["python3", "-c", _INSTALL],
            {**payload, "_worker_source": source}, **options,
        )
        result = json.loads(output)
    return result
