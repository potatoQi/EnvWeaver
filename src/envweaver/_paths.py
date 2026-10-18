"""Parse local paths and SSH file addresses."""

from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from ._ssh_transport import _validate_host


def _location(root):
    parsed = urlsplit(root)
    if parsed.scheme != "ssh":
        raise ValueError(f"Unsupported file address scheme: {parsed.scheme}")
    host = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    _validate_host(host)
    path = unquote(parsed.path)
    if parsed.query or parsed.fragment or not path.startswith("/") or "\x00" in path:
        raise ValueError("SSH address must include an absolute path, without query or fragment")
    return host, path


def normalize_address(root: str | Path) -> str | Path:
    if isinstance(root, str):
        if not root.strip():
            raise ValueError("File address must be nonempty")
        if "://" in root:
            host, path = _location(root)
            return host + quote(path, safe="/")
    return Path(root).expanduser().absolute()
