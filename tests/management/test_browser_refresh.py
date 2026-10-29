"""Browser checks for response ordering and bounded background refresh."""

import os
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from tests.management.helpers import configured_app
from tests.management.test_api import FakeFiles, package

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("ENVWEAVER_MANAGEMENT_BROWSER") != "1", reason="opt-in installed Chromium"
    ),
]


def test_refresh_does_not_stack_or_replace_newer_search(tmp_path):
    app = configured_app(tmp_path / "managed", environment_files=FakeFiles())
    root = tmp_path / "managed/task-pool"
    for name in ("alpha", "beta"):
        package(root / name, name)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ["ENVWEAVER_CHROMIUM"], args=["--no-sandbox"]
            )
            page = browser.new_page()
            page.add_init_script("""
                window.apiCalls = [];
                const fetchOriginal = window.fetch;
                window.fetch = async (...args) => {
                    const path = new URL(args[0], location.origin).pathname;
                    apiCalls.push({path, at: performance.now()});
                    const response = await fetchOriginal(...args);
                    if (path.endsWith('/assets/page') &&
                        JSON.parse(args[1].body).selection.query === 'alpha') {
                        // An old response may already have arrived when a newer search starts.
                        const body = await response.text();
                        await new Promise(resolve => setTimeout(resolve, 1200));
                        return new Response(body, {status: response.status});
                    }
                    if (path.endsWith('/index') && window.slowIndex)
                        await new Promise(resolve => setTimeout(resolve, 4000));
                    return response;
                };
            """)
            page.goto(f"http://127.0.0.1:{port}")
            expect(page.locator("#count")).to_have_text("2 条任务")
            page.wait_for_timeout(6500)
            assert page.evaluate("apiCalls.filter(c=>c.path.endsWith('/assets/page')).length") == 1
            page.locator("#query").fill("alpha")
            page.wait_for_function(
                "apiCalls.filter(c=>c.path.endsWith('/assets/page')).length === 2"
            )
            page.locator("#query").fill("beta")
            expect(page.locator("#count")).to_have_text("1 条任务")
            expect(page.locator("#rows")).to_contain_text("beta")
            page.wait_for_timeout(1500)
            expect(page.locator("#rows")).not_to_contain_text("alpha")
            page.locator("#query").fill("")
            expect(page.locator("#count")).to_have_text("2 条任务")
            package(root / "gamma", "gamma")
            expect(page.locator("#count")).to_have_text("3 条任务", timeout=10000)
            # A hidden page stops starting new background requests.
            page.evaluate(
                "Object.defineProperty(document, 'hidden', {configurable:true, get:()=>true})"
            )
            page.wait_for_timeout(500)
            count = page.evaluate("apiCalls.filter(c=>c.path.endsWith('/index')).length")
            page.wait_for_timeout(3500)
            assert page.evaluate("apiCalls.filter(c=>c.path.endsWith('/index')).length") == count
            page.evaluate("""() => {
                delete document.hidden;
                window.slowIndex = true;
                window.apiCalls = [];
                document.dispatchEvent(new Event('visibilitychange'));
            }""")
            page.wait_for_timeout(7600)
            times = page.evaluate("apiCalls.filter(c=>c.path.endsWith('/index')).map(c=>c.at)")
            assert len(times) == 2
            assert times[1] - times[0] >= 6900
            # Slow file listings must not delay images or another detail navigation.
            page.evaluate("""() => {
                const previous = window.fetch;
                window.detailCalls = [];
                window.fetch = async (...args) => {
                    const path = new URL(args[0], location.origin).pathname;
                    detailCalls.push(path);
                    const response = await previous(...args);
                    if (path.endsWith('/files')) {
                        const body = await response.text();
                        await new Promise(resolve => setTimeout(resolve, 3000));
                        return new Response(body, {status: response.status});
                    }
                    return response;
                };
            }""")
            names = page.locator("#rows [data-open]")
            other_id = names.nth(1).get_attribute("data-open")
            other_name = names.nth(1).inner_text()
            names.first.click()
            page.wait_for_function("document.querySelector('#detail img')?.naturalWidth > 0")
            expect(page.locator("#detail #files")).to_have_text("加载中…")
            expect(page.locator("#detail details .json-view")).to_have_count(0)
            page.locator("#detail summary").click()
            expect(page.locator("#detail details .json-view")).to_be_visible()
            assert not page.evaluate("detailCalls.some(path=>path === '/api/tags')")
            page.evaluate(
                """async id => {
                const {openAsset} = await import('/src/pages/assets.js');
                void openAsset(id);
            }""",
                other_id,
            )
            expect(page.locator("#detail h2")).to_have_text(other_name)
            page.locator("#detail-close").click()
            page.wait_for_timeout(3200)
            expect(page.locator("#detail")).not_to_be_visible()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()
