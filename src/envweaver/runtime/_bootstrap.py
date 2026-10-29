"""Bootstrap Python using APT's own resolution and the shared download cache."""

import re
import shlex
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from ..cache import DownloadCache


def apt_downloads(plan):
    """Return only checksum-addressed archives selected by apt-get --print-uris."""
    for line in plan.splitlines():
        if not line.startswith("'"):
            continue
        fields = shlex.split(line)
        if len(fields) != 4:
            raise ValueError("Invalid APT download plan")
        url, filename, size, checksum = fields
        if Path(filename).name != filename or not filename.endswith(".deb"):
            raise ValueError("Invalid APT archive filename")
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or parsed.username is not None
                or not re.fullmatch(r"sha256:[0-9a-f]{64}|sha512:[0-9a-f]{128}", checksum.lower())):
            continue
        yield url, filename, int(size), checksum.lower()


def prepare_python(docker, cid, python, install_command, env, *, prepare_command="", timeout=300):
    script = Path(__file__).with_name("_python.sh").read_text()
    deadline = time.monotonic() + timeout

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Python bootstrap timed out")
        return seconds

    def execute(prefix):
        return docker("exec", cid, "/bin/sh", "-c", prefix + script,
                      "envweaver-python", python, install_command, prepare_command,
                      timeout=remaining())

    output = execute("mode=plan\n")
    marker = "ENVWEAVER_APT_DOWNLOADS\n"
    if marker not in output:
        return
    packages = list(apt_downloads(output.split(marker, 1)[1]))
    cache = DownloadCache()
    proxies = {scheme: env.get(scheme + "_proxy", env.get(scheme.upper() + "_PROXY"))
               for scheme in ("http", "https")}
    proxies = {key: value for key, value in proxies.items() if value}
    cancelled = threading.Event()
    with tempfile.TemporaryDirectory(prefix="envweaver-apt-") as directory:
        folder = Path(directory)
        folder.chmod(0o755)

        def materialize(package):
            url, filename, size, checksum = package
            target = folder / filename
            try:
                cache._download(url=url, checksum=checksum, target=target,
                                proxies=proxies or None, cancelled=cancelled)
            except InterruptedError:
                raise TimeoutError("Python bootstrap timed out") from None
            except OSError:
                # Private repositories or sandbox-only routes remain APT's responsibility.
                return
            if target.stat().st_size != size:
                raise ValueError("APT archive size differs from download plan")
            target.chmod(0o644)

        timer = threading.Timer(remaining(), cancelled.set)
        timer.start()
        try:
            with ThreadPoolExecutor(max_workers=8) as workers:
                list(workers.map(materialize, packages))
        finally:
            cancelled.set()
            timer.cancel()
            timer.join()
        remote = "/tmp/envweaver-apt-" + uuid.uuid4().hex
        docker("cp", str(folder), cid + ":" + remote, timeout=remaining())
        execute("mode=finish\narchives=" + shlex.quote(remote) + "\n")
