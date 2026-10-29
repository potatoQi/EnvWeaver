"""Controller-owned, checksum-addressed downloads. No sandbox cache is imported."""

import hashlib
import http.client
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


class DownloadCache:
    """
    Args:
        root: 当前机器上的缓存目录；默认 ENVWEAVER_CACHE_DIR，未设置时为 ~/.cache/envweaver/downloads。
        max_bytes: 缓存容量上限，默认 30 GiB；超限时清理最近未使用的文件。

    Raises:
        ValueError: max_bytes 不是正整数。
    """

    def __init__(self, root: str | Path | None = None, *, max_bytes: int = 30 * 1024**3):
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self.root = (
            Path(
                root
                or os.environ.get("ENVWEAVER_CACHE_DIR", Path.home() / ".cache/envweaver/downloads")
            )
            .expanduser()
            .absolute()
        )
        self.max_bytes = max_bytes

    def download(
        self,
        *,
        url: str,
        checksum: str,
        target: str | Path,
        proxies: dict[str, str] | None = None,
    ) -> None:
        """
        Args:
            url: HTTP 或 HTTPS 文件地址，不接受包含用户名或密码的地址。
            checksum: 文件校验值，格式为 sha256:十六进制值或 sha512:十六进制值。
            target: 当前机器上的目标文件；成功后原子覆盖，不与缓存共享可写文件。
            proxies: HTTP、HTTPS 代理地址字典；默认 None，读取当前进程的代理配置。

        Returns:
            None。

        Raises:
            ValueError: 地址或校验值无效，或下载内容与校验值不符。
            OSError: 下载或文件访问失败；网络失败最多尝试三次。
        """
        self._download(url=url, checksum=checksum, target=target, proxies=proxies)

    def _download(self, *, url, checksum, target, proxies=None, cancelled=None):
        import fcntl

        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Expected an HTTP(S) download URL without credentials")
        if not isinstance(checksum, str) or not re.fullmatch(
            r"sha256:[0-9a-fA-F]{64}|sha512:[0-9a-fA-F]{128}", checksum
        ):
            raise ValueError("Expected sha256:hex or sha512:hex checksum")
        algorithm, digest = checksum.lower().split(":")
        key = hashlib.sha256((url + "\n" + checksum.lower()).encode()).hexdigest()
        target = Path(target)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.parent.mkdir(parents=True, exist_ok=True)
        artifact = self.root / (key + ".data")
        if target.absolute() == artifact:
            raise ValueError("target must be outside the cache artifact")

        def check_cancelled():
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("Download cancelled")

        def valid():
            if not artifact.is_file():
                return False
            with artifact.open("rb") as stream:
                return hashlib.file_digest(stream, algorithm).hexdigest() == digest

        # Lock files are never unlinked: replacing a lock inode breaks interprocess exclusion.
        with (self.root / (key + ".lock")).open("a") as lock:
            while True:
                check_cancelled()
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(0.05)
            if not valid():
                artifact.unlink(missing_ok=True)
                for stale in self.root.glob(key + ".part-*"):
                    stale.unlink(missing_ok=True)
                with tempfile.NamedTemporaryFile(
                    dir=self.root, prefix=key + ".part-", delete=False
                ) as stream:
                    temporary = Path(stream.name)
                try:
                    self._retrieve(
                        url, temporary, algorithm, digest, proxies, check_cancelled, cancelled
                    )
                    temporary.replace(artifact)
                finally:
                    temporary.unlink(missing_ok=True)
            check_cancelled()
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                temporary = Path(stream.name)
            try:
                shutil.copyfile(artifact, temporary)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            artifact.touch()
        self._trim()

    @staticmethod
    def _retrieve(url, path, algorithm, digest, proxies, check_cancelled, cancelled):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        for attempt in range(3):
            check_cancelled()
            offset = path.stat().st_size
            request = urllib.request.Request(
                url, headers={"User-Agent": "envweaver", **({"Range": f"bytes={offset}-"} if offset else {})}
            )
            try:
                with opener.open(request, timeout=30) as response:
                    resumed = offset > 0 and response.status == 206
                    if response.status == 206 and not response.headers.get(
                        "Content-Range", ""
                    ).startswith(f"bytes {offset}-"):
                        raise OSError("Invalid download range")
                    count = 0
                    with path.open("ab" if resumed else "wb") as stream:
                        while chunk := response.read(64 * 1024):
                            check_cancelled()
                            stream.write(chunk)
                            count += len(chunk)
                    length = response.headers.get("Content-Length")
                    if length is not None and count != int(length):
                        raise OSError("Incomplete download")
                with path.open("rb") as stream:
                    if hashlib.file_digest(stream, algorithm).hexdigest() != digest:
                        raise ValueError("Download checksum mismatch")
                return
            except InterruptedError:
                raise
            except (OSError, http.client.HTTPException) as error:
                if attempt == 2:
                    if isinstance(error, http.client.HTTPException):
                        raise OSError("Download response interrupted") from error
                    raise
                if isinstance(error, urllib.error.HTTPError):
                    if error.code < 500 and error.code not in {408, 429}:
                        raise
                if cancelled is not None:
                    if cancelled.wait(0.5 * 2**attempt):
                        check_cancelled()
                else:
                    time.sleep(0.5 * 2**attempt)

    def _trim(self):
        import fcntl

        files = []
        for path in self.root.glob("*.data"):
            try:
                stat = path.stat()
                files.append((stat.st_mtime, stat.st_size, path))
            except FileNotFoundError:
                pass
        total = sum(size for _, size, _ in files)
        for _, size, path in sorted(files):
            if total <= self.max_bytes:
                break
            with path.with_suffix(".lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                path.unlink(missing_ok=True)
                total -= size
