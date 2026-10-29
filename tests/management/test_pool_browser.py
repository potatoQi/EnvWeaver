"""Browser regression: pool selection and automatic SDK file discovery."""

import json
import os
import socket
import threading
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("playwright")
import uvicorn
from playwright.sync_api import expect, sync_playwright

from envweaver.management.api import create_app
from tests.management.test_api import REF, FakeFiles, package

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("ENVWEAVER_MANAGEMENT_BROWSER") != "1", reason="opt-in Chromium"
    ),
]


def test_pool_selection_and_live_discovery(tmp_path):
    root = tmp_path
    pool = root / "SDK Task Pool"
    package(pool / "saved-task", name="saved-task")
    runtime_pool = root / "SDK Runtime Pool"
    record = runtime_pool / "runtimes/first-snapshot"
    record.mkdir(parents=True)
    (record / "runtime.json").write_text(json.dumps(REF))
    app = create_app(environment_files=FakeFiles())
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                executable_path=os.environ.get("ENVWEAVER_CHROMIUM"),
                headless=True,
                args=["--no-sandbox"],
            )
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}")
            expect(page.locator("#pool-empty")).to_be_visible()

            def select_pool(address):
                page.locator("#choose-pool").click()
                page.locator("#modal input[name=address]").fill(str(address))
                page.locator("#modal button[type=submit]").click()
                expect(page.locator("#modal")).not_to_be_visible(timeout=10000)

            select_pool(pool)
            expect(page.locator("#count")).to_have_text("1 条任务")
            expect(page.locator("#pool-select")).to_have_count(0)
            expect(page.locator("#pool-path")).to_have_text(str(pool))
            # A file published after the page opened appears through polling, without refresh.
            package(pool / "saved-later", name="saved-later")
            expect(page.locator("#count")).to_have_text("2 条任务", timeout=12000)
            page.reload()
            expect(page.locator("#count")).to_have_text("2 条任务")
            expect(page.locator("#pool-path")).to_have_text(str(pool))
            page.screenshot(path=str(tmp_path / "desktop.png"), full_page=True)
            page.locator("[data-kind=runtime]").click()
            select_pool(runtime_pool)
            expect(page.locator("#count")).to_have_text("1 条环境")
            page.screenshot(path=str(tmp_path / "runtime.png"), full_page=True)
            page.locator("[data-kind=task]").click()
            expect(page.locator("#count")).to_have_text("2 条任务")
            empty_pool = root / "empty-pool"
            empty_pool.mkdir()
            select_pool(empty_pool)
            expect(page.locator("#count")).to_have_text("0 条任务")
            page.reload()
            expect(page.locator("#pool-path")).to_have_text(str(empty_pool))
            expect(page.locator("#count")).to_have_text("0 条任务")
            select_pool(pool)
            expect(page.locator("#count")).to_have_text("2 条任务")
            fresh = browser.new_page()
            fresh.goto(f"http://127.0.0.1:{port}")
            expect(fresh.locator("#count")).to_have_text("2 条任务")
            fresh.locator("[data-kind=runtime]").click()
            expect(fresh.locator("#pool-path")).to_have_text(str(runtime_pool))
            expect(fresh.locator("#count")).to_have_text("1 条环境")
            fresh.close()
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(tmp_path / "mobile.png"), full_page=True)
            page.locator("#choose-pool").click()
            page.screenshot(path=str(tmp_path / "dialog-mobile.png"), full_page=True)
            assert not errors, errors
            print(
                "Pool selection, switching, automatic discovery, service-scoped selection, responsive UI: passed"
            )
            browser.close()
    finally:
        server.should_exit = True
        thread.join(10)
