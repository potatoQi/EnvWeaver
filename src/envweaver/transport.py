import os

import httpx

from ._files import pack, unpack
from .errors import RuntimeError


class GuestClient:
    """Connection to envweaver-guest inside a prepared desktop/VM."""

    def __init__(self, instance):
        data = instance.data
        config = data.get("ref", {}).get("config", {})
        self.url = data.get("guest_url") or config.get("guest_url")
        if not self.url:
            raise ValueError(
                "This Runtime needs a configured guest_url for IPython/guest operations"
            )
        token_env = data.get("token_env") or config.get("token_env", "ENVWEAVER_GUEST_TOKEN")
        self.token = os.environ.get(token_env)
        if not self.token:
            raise ValueError(f"Missing guest credential in {token_env}")

    def request(self, method, path, *, timeout=300, **kwargs):
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.request(
                method,
                self.url.rstrip("/") + path,
                headers={"Authorization": f"Bearer {self.token}", **kwargs.pop("headers", {})},
                **kwargs,
            )
        if response.is_error:
            try:
                message = response.json()
            except ValueError:
                message = response.text[:2000]
            raise RuntimeError(f"Guest {path} failed (HTTP {response.status_code}): {message}")
        return response

    def health(self):
        return self.request("GET", "/health", timeout=2).json()

    def exec(self, command, cwd=None, timeout=300):
        result = self.request(
            "POST",
            "/exec",
            timeout=timeout + 15,
            json={"command": command, "cwd": cwd, "timeout": timeout},
        ).json()
        if result.get("error") == "timeout":
            raise TimeoutError(result["message"])
        if "error" in result:
            raise RuntimeError(result["message"])
        return result["exit_code"], result["stdout"], result["stderr"]

    def upload(self, local_path, remote_path):
        kind, body = pack(local_path)
        self.request(
            "POST",
            "/files",
            params={"path": remote_path},
            content=body,
            headers={"X-EnvWeaver-Kind": kind},
        )

    def download(self, remote_path, local_path):
        response = self.request("GET", "/files", params={"path": remote_path})
        unpack(response.headers["X-EnvWeaver-Kind"], response.content, local_path)

    def ipython(self, code, session, timeout=300):
        return self.request(
            "POST",
            "/ipython",
            timeout=timeout + 20,
            json={"code": code, "session": session, "timeout": timeout},
        ).json()

    def close_session(self, session):
        self.request("DELETE", "/ipython", params={"session": session}, timeout=15)
