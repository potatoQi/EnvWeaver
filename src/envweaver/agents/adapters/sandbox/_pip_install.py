"""Keep pip's resolver and installer; delegate only hash-identified downloads."""

import importlib
import os
import runpy
from pathlib import Path
from urllib.parse import urlsplit

from _download_client import download


def main():
    # Load pip in its install-command order before patching the downloader.
    importlib.import_module("pip._internal.commands.install")

    from pip._internal.network.download import Downloader
    from pip._vendor.requests import Request

    original = Downloader.__call__

    def cached_download(self, link, location):
        if link.hash_name not in {"sha256", "sha512"} or not link.hash:
            return original(self, link, location)
        request = self._session.prepare_request(Request("GET", link.url_without_fragment))
        if urlsplit(link.url_without_fragment).username or any(
            key in request.headers for key in ("Authorization", "Cookie")
        ):
            return original(self, link, location)
        filename = link.filename
        if Path(filename).name != filename:
            raise ValueError("Invalid package filename")
        target = os.path.join(location, filename)
        if not download(link.url_without_fragment, f"{link.hash_name}:{link.hash}", target):
            return original(self, link, location)
        return target, "application/octet-stream"


    Downloader.__call__ = cached_download
    runpy.run_module("pip", run_name="__main__")


if __name__ == "__main__":
    main()
