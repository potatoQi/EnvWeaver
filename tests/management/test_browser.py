"""Opt-in browser acceptance for both asset tabs and responsive dialogs."""

import json
import os
import socket
import struct
import threading
import time
import zlib

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("playwright")
import uvicorn
from playwright.sync_api import expect, sync_playwright

from tests.management.helpers import configured_app
from tests.management.test_api import REF, FakeFiles, package

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("ENVWEAVER_MANAGEMENT_BROWSER") != "1", reason="opt-in installed Chromium"
    ),
]


def test_browser_asset_workflows(tmp_path):
    app = configured_app(
        tmp_path / "managed",
        environment_files=FakeFiles(),
    )
    for name, tags in (("browser-task", ["benchmark", "review"]), ("second-task", ["benchmark"])):
        folder = package(tmp_path / "managed/task-pool" / name, name)
        path = folder / "task.json"
        manifest = json.loads(path.read_text())
        manifest["metadata"]["tags"] = tags
        path.write_text(json.dumps(manifest))

    # A large image exercises fitted previews and a centered image dialog.
    def png_chunk(kind, data):
        return (
            struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
        )

    pixels = b"".join(b"\x00" + bytes((40 + y % 160, 110, 220)) * 1600 for y in range(1000))
    (tmp_path / "managed/task-pool/browser-task/goal.png").write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack("!2I5B", 1600, 1000, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(pixels))
        + png_chunk(b"IEND", b"")
    )
    (tmp_path / "managed/task-pool/browser-task/notes.txt").write_text("Existing notes")
    from envweaver._io.storage import write_records

    write_records(
        tmp_path / "managed/runtime-pool",
        "runtimes/test-one",
        {
            "runtime.json": {**REF, "name": "test-one"},
            "metadata.json": {"host": None},
        },
    )
    app.state.pools.sync()
    with app.state.store.transaction():
        for row in app.state.store.assets.values():
            if row["kind"] == "task":
                row["created_by"] = "alice" if row["name"] == "browser-task" else "bob"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=os.environ.get("ENVWEAVER_CHROMIUM"),
                headless=True,
                args=["--no-sandbox"],
            )
            page = browser.new_page(
                viewport={"width": 1440, "height": 1000}, timezone_id="Asia/Shanghai"
            )
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{port}")
            expect(page.locator("#modal")).not_to_be_visible()
            page.locator("#choose-pool").click()
            page.locator("#modal input[name=address]").fill(str(tmp_path / "managed/task-pool"))
            page.locator("#modal button[type=submit]").click()
            expect(page.locator("#modal")).not_to_be_visible()
            expect(page.locator("#count")).to_have_text("2 条任务")
            expect(page.locator("thead th")).to_have_count(8)
            for key in ("created_by", "created_at", "updated_by", "updated_at"):
                assert page.locator(f'#rows td[data-column="{key}"]').first.inner_text()
            name_column = page.locator('col[data-column="name"]')
            initial_name_width = name_column.bounding_box()["width"]
            grip = page.get_by_role("button", name="调整名称列宽", exact=True).bounding_box()
            page.mouse.move(grip["x"] + 4, grip["y"] + 12)
            page.mouse.down()
            page.mouse.move(grip["x"] + 124, grip["y"] + 12, steps=8)
            page.mouse.up()
            task_name_width = name_column.bounding_box()["width"]
            assert abs(task_name_width - initial_name_width - 120) < 2
            page.locator('[data-kind="runtime"]').click()
            expect(page.locator("#count")).to_have_text("1 条环境")
            assert abs(name_column.bounding_box()["width"] - initial_name_width) < 2
            page.get_by_role("button", name="调整名称列宽", exact=True).focus()
            page.keyboard.press("ArrowRight")
            runtime_name_width = name_column.bounding_box()["width"]
            page.locator('[data-kind="task"]').click()
            expect(page.locator("#count")).to_have_text("2 条任务")
            assert abs(name_column.bounding_box()["width"] - task_name_width) < 2
            page.reload()
            expect(page.locator("#count")).to_have_text("2 条任务")
            assert abs(name_column.bounding_box()["width"] - task_name_width) < 2
            page.locator('[data-kind="runtime"]').click()
            expect(page.locator("#count")).to_have_text("1 条环境")
            assert abs(name_column.bounding_box()["width"] - runtime_name_width) < 2
            page.locator('[data-kind="task"]').click()
            expect(page.locator("#count")).to_have_text("2 条任务")
            table_wrap = page.locator(".table-wrap")
            page.locator("#rows .asset-name").first.hover()
            page.keyboard.down("Shift")
            page.mouse.wheel(0, 280)
            page.keyboard.up("Shift")
            page.wait_for_function("document.querySelector('.table-wrap').scrollLeft > 0")
            table_wrap.evaluate("el => el.scrollLeft = 0")
            for value, label in (
                ("created_at:desc", "最近创建"),
                ("name:asc", "名称 A → Z"),
                ("updated_at:desc", "最近修改"),
            ):
                page.locator("#sort").click()
                expect(page.locator('.popup-menu input[type="radio"]')).to_have_count(3)
                expect(page.locator(".popup-menu input:checked")).to_have_count(1)
                with page.expect_response(
                    lambda response: (
                        "/api/assets/page" in response.url and response.request.method == "POST"
                    )
                ) as sorted_response:
                    page.locator(f'.popup-menu input[value="{value}"]').click()
                body = sorted_response.value.request.post_data_json
                assert body["sort"] == value.split(":")[0]
                assert body["descending"] == value.endswith(":desc")
                expect(page.locator(".popup-menu")).to_have_count(0)
                expect(page.locator("#sort")).to_have_text(f"{label} ▾")
            page.locator("#rows .asset-name").first.click()
            panel = page.locator("#detail")
            expect(panel).to_be_visible()
            edge = page.locator(".detail-resize-handle").bounding_box()
            original_width = panel.bounding_box()["width"]
            page.mouse.move(edge["x"] + 3, 120)
            page.mouse.down()
            page.mouse.move(edge["x"] - 197, 120, steps=10)
            page.mouse.up()
            expect(panel).to_be_visible()
            saved_width = panel.bounding_box()["width"]
            assert abs(saved_width - original_width - 200) < 2
            # Interior clicks and selection dragged outside must not dismiss it.
            page.locator("#detail h2").click()
            expect(panel).to_be_visible()
            heading = page.locator("#detail h2").bounding_box()
            page.mouse.move(heading["x"] + 5, heading["y"] + 5)
            page.mouse.down()
            page.mouse.move(20, 180, steps=4)
            page.mouse.up()
            expect(panel).to_be_visible()
            page.mouse.click(20, 180)
            expect(panel).not_to_be_visible()
            page.reload()
            page.locator("#rows .asset-name").first.click()
            expect(panel).to_be_visible()
            assert abs(panel.bounding_box()["width"] - saved_width) < 2
            page.locator("#detail [data-related]").first.click()
            expect(page.locator("#detail-back")).to_be_visible()
            assert abs(panel.bounding_box()["width"] - saved_width) < 2
            # Width fits a small screen without overwriting the desktop preference.
            page.set_viewport_size({"width": 390, "height": 844})
            assert panel.bounding_box()["width"] <= 390
            page.set_viewport_size({"width": 1440, "height": 1000})
            assert abs(panel.bounding_box()["width"] - saved_width) < 2
            # The resize edge remains usable after the detail body scrolls.
            panel.evaluate("el => el.scrollTop = el.scrollHeight")
            handle = page.locator(".detail-resize-handle")
            handle.focus()
            page.keyboard.press("ArrowRight")
            assert abs(panel.bounding_box()["width"] - saved_width + 32) < 2
            page.keyboard.press("Home")
            assert abs(panel.bounding_box()["width"] - 360) < 2
            page.keyboard.press("End")
            assert abs(panel.bounding_box()["width"] - 1440 * 0.94) < 2
            page.mouse.click(20, 180)
            expect(panel).not_to_be_visible()
            expect(page.locator("#advanced")).not_to_be_visible()
            expect(page.locator("#host-filter")).to_have_count(0)
            page.locator("#creator-filter").click()
            page.locator('.popup-menu input[value="alice"]').check()
            expect(page.locator("#count")).to_have_text("1 条任务")
            page.locator('.popup-menu input[value="bob"]').check()
            expect(page.locator("#count")).to_have_text("2 条任务")
            page.screenshot(path="/tmp/envweaver-creators-desktop.png")
            page.locator(".popup-menu input[data-all]").check()
            expect(page.locator("#creator-filter")).to_have_text("全部创建者 ▾")
            page.keyboard.press("Escape")
            page.locator("#more-filters").click()
            fields = ("created-after", "created-before", "updated-after", "updated-before")
            for field in fields:
                page.locator(f"#{field}-filter").fill("2026-01-01")
            with page.expect_response(
                lambda response: (
                    "/api/assets/page" in response.url and response.request.method == "POST"
                )
            ) as filtered:
                page.locator("#apply-filters").click()
            selected = filtered.value.request.post_data_json["selection"]
            assert (
                selected["created_after"] == selected["updated_after"] == "2025-12-31T16:00:00.000Z"
            )
            assert (
                selected["created_before"]
                == selected["updated_before"]
                == "2026-01-01T15:59:59.999Z"
            )
            page.screenshot(path="/tmp/envweaver-time-filters-desktop.png", full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            expect(page.locator("#updated-before-filter")).to_be_visible()
            page.screenshot(path="/tmp/envweaver-time-filters-mobile.png", full_page=True)
            page.set_viewport_size({"width": 1440, "height": 1000})
            for field in fields:
                page.locator(f"#{field}-filter").fill("")
            page.locator("#apply-filters").click()
            expect(page.locator("#count")).to_have_text("2 条任务")
            page.locator("#more-filters").click()
            # Searching tag names only filters the sidebar, without requests or row changes.
            page.locator("#rows input").first.check()
            requests = page.evaluate("""() => {
                const original = window.fetch;
                const requests = [];
                window.fetch = (...args) => { requests.push(String(args[0])); return original(...args); };
                try {
                    const search = document.querySelector('#tag-search');
                    search.value = 'review';
                    search.dispatchEvent(new Event('input'));
                    return requests;
                } finally { window.fetch = original; }
            }""")
            assert requests == []
            expect(page.locator("#tag-list input")).to_have_count(1)
            expect(page.locator("#tag-list input")).to_have_value("review")
            expect(page.locator("#count")).to_have_text("2 条任务")
            expect(page.locator("#selected-label")).to_have_text(" · 已选 1 条")
            page.locator("#tag-search").fill("")
            expect(page.locator("#tag-list input")).to_have_count(2)
            page.locator("#clear-selection").click()

            # Multiple backend choices combine with OR; new catalog names appear automatically.
            def extra_backends(route):
                response = route.fetch()
                body = response.json()
                body["backends"] = ["docker", "qemu", "kube"]
                route.fulfill(response=response, json=body)

            page.route("**/api/assets/page*", extra_backends)
            # Wait for the discovery request to complete before opening the menu.
            with page.expect_response("**/api/assets/page*"):
                page.locator("#refresh").click()
            page.locator("#backend-filter").click()
            page.locator('.popup-menu input[value="kube"]').check()
            expect(page.locator("#count")).to_have_text("0 条任务")
            page.locator('.popup-menu input[value="docker"]').check()
            expect(page.locator("#count")).to_have_text("2 条任务")
            expect(page.locator('.popup-menu input[value="kube"]')).to_be_checked()
            expect(page.locator('.popup-menu input[value="docker"]')).to_be_checked()
            expect(page.locator(".popup-menu [data-all]")).not_to_be_checked()
            page.screenshot(path="/tmp/envweaver-backends-desktop.png", full_page=True)
            page.keyboard.press("Escape")
            page.set_viewport_size({"width": 390, "height": 844})
            page.locator("#backend-filter").click()
            page.screenshot(path="/tmp/envweaver-backends-mobile.png", full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.locator(".popup-menu [data-all]").check()
            expect(page.locator("#backend-filter")).to_have_text("全部后端 ▾")
            expect(page.locator('.popup-menu input[value="docker"]')).not_to_be_checked()
            page.keyboard.press("Escape")
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.unroute("**/api/assets/page*", extra_backends)
            # A page contains one record; header selection must still include both.
            page.locator("#page-size").evaluate("el => el.add(new Option('1', '1'))")
            page.locator("#page-size").select_option("1")
            expect(page.locator("#rows tr")).to_have_count(1)
            page.locator("#check-all").check()
            expect(page.locator("#selected-label")).to_have_text(" · 已选 2 条")
            page.locator('#page-links [data-page="2"]').click()
            expect(page.locator("#page-info")).to_have_text("第 2 / 2 页")
            expect(page.locator("#rows input[type=checkbox]")).to_be_checked()
            excluded = page.locator("#rows input").get_attribute("data-id")
            page.locator("#rows input").uncheck()
            expect(page.locator("#selected-label")).to_have_text(" · 已选 1 条")
            assert page.locator("#check-all").evaluate("el => el.indeterminate")
            page.locator('#page-links [data-page="1"]').click()
            expect(page.locator("#page-info")).to_have_text("第 1 / 2 页")
            expect(page.locator("#rows input")).to_be_checked()
            page.locator("#add-tag").click()
            page.locator("#modal [name=tag]").fill("across-pages")
            page.locator("#modal button[type=submit]").click()
            expect(page.locator("#modal")).not_to_be_visible()
            page.locator("#operations").click()
            page.locator("#batch-tags").click()
            page.locator('#modal input[value="across-pages"]').check()
            page.locator("#modal button[type=submit]").click()
            expect(page.locator("#modal")).not_to_be_visible()
            assert "across-pages" not in app.state.store.get(excluded)["tags"]
            included = page.locator("#rows input").get_attribute("data-id")
            assert "across-pages" in app.state.store.get(included)["tags"]
            page.locator("#check-all").check()
            page.locator("#check-all").uncheck()
            expect(page.locator("#operations")).to_be_disabled()
            page.locator("#page-size").select_option("30")
            expect(page.locator("#rows tr")).to_have_count(2)
            page.locator("#tag-list input[value=review]").check()
            expect(page.locator("#count")).to_have_text("1 条任务")
            page.locator("#rows .asset-name").click()
            expect(page.locator("#detail h2")).to_have_text("browser-task")
            expect(page.locator("#detail dt").filter(has_text="服务器")).to_have_count(0)
            # Follow Task -> Runtime -> Task, then return through both levels.
            expect(page.locator("#detail-back")).to_have_count(0)
            relation = page.locator("#detail [data-related]").first
            relation.scroll_into_view_if_needed()
            task_scroll = page.locator("#detail").evaluate("el => el.scrollTop")
            relation.click()
            expect(page.locator("#detail h3").filter(has_text="引用该环境的 Task")).to_be_visible()
            runtime_name = page.locator("#detail h2").inner_text()
            page.locator("#detail [data-related]").filter(has_text="browser-task").first.click()
            expect(page.locator("#detail h2")).to_have_text("browser-task")
            page.locator("#detail summary").click()
            page.locator("#detail").evaluate("el => el.scrollTop = 500")
            page.reload()
            expect(page.locator("#detail h2")).to_have_text("browser-task")
            expect(page.locator("#detail details")).to_have_attribute("open", "")
            page.wait_for_function(
                "Math.abs(document.querySelector('#detail').scrollTop - 500) < 2"
            )
            page.locator("#detail-back").click()
            expect(page.locator("#detail h2")).to_have_text(runtime_name)
            page.set_viewport_size({"width": 390, "height": 844})
            page.locator("#detail-back").scroll_into_view_if_needed()
            page.screenshot(path="/tmp/envweaver-detail-back-mobile.png")
            assert page.locator("#detail").evaluate("el => el.scrollWidth <= el.clientWidth")
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.locator("#detail-back").click()
            expect(page.locator("#detail h2")).to_have_text("browser-task")
            expect(page.locator("#detail-back")).to_have_count(0)
            page.wait_for_function("document.querySelector('.goal-image').naturalWidth > 0")
            page.wait_for_function(
                "top => Math.abs(document.querySelector('#detail').scrollTop - top) < 2",
                arg=task_scroll,
            )
            page.locator("#detail").evaluate("el => el.scrollTop = 0")
            page.screenshot(path="/tmp/envweaver-detail-back-desktop.png")
            expect(page.locator(".goal-image")).to_be_visible()
            page.wait_for_function("document.querySelector('.goal-image').naturalWidth > 0")
            image = page.locator(".goal-image")
            assert image.bounding_box()["height"] <= 330
            image.click()
            viewer = page.locator(".image-viewer")
            expect(viewer).to_be_visible()
            page.screenshot(path="/tmp/envweaver-image-viewer-desktop.png")
            expect(viewer.locator(".image-size")).to_have_count(0)
            bounds = viewer.bounding_box()
            assert abs(bounds["x"] + bounds["width"] / 2 - 720) <= 1
            assert abs(bounds["y"] + bounds["height"] / 2 - 500) <= 1
            image_bounds = viewer.locator("img").bounding_box()
            assert image_bounds["width"] < 1600
            assert abs(image_bounds["width"] / image_bounds["height"] - 1.6) < 0.01
            page.keyboard.press("Escape")
            expect(viewer).to_have_count(0)
            expect(page.locator("#detail")).to_be_visible()
            page.set_viewport_size({"width": 390, "height": 844})
            image.focus()
            page.keyboard.press("Enter")
            expect(viewer).to_be_visible()
            assert viewer.locator("img").bounding_box()["width"] <= 390
            page.screenshot(path="/tmp/envweaver-image-viewer-mobile.png")
            page.get_by_role("button", name="关闭图片").click()
            expect(viewer).to_have_count(0)
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.locator("#edit-asset").click()
            expect(page.locator('#modal [name="host"]')).to_have_count(0)
            page.locator("#modal [name=tags]").fill("benchmark, review, updated")
            page.locator("#modal button[type=submit]").click()
            expect(page.locator("#modal")).not_to_be_visible()
            expect(page.locator("#detail .chip").filter(has_text="updated")).to_be_visible()
            # Editing metadata.tags in Task JSON is not overwritten by an unchanged tag input.
            page.locator("#edit-asset").click()
            spec = json.loads(page.locator("#modal [name=spec]").input_value())
            spec["metadata"]["tags"].append("json-tag")
            page.locator("#modal [name=spec]").fill(json.dumps(spec))
            page.locator("#modal button[type=submit]").click()
            expect(page.locator("#modal")).not_to_be_visible()
            expect(page.locator("#detail .chip").filter(has_text="json-tag")).to_be_visible()
            expect(page.locator("#add-file")).to_have_count(0)
            page.locator('[data-file="notes.txt"]').click()
            expect(page.locator('#modal input[type="file"]')).to_have_count(0)
            expect(page.locator('#modal [name="path"]')).to_have_count(0)
            page.locator("#modal [name=content]").fill("Browser edited this file")
            page.locator("#modal button[type=submit]").click()
            expect(page.locator("#modal")).not_to_be_visible()
            page.locator('[data-file="notes.txt"]').click()
            expect(page.locator("#modal [name=content]")).to_have_value("Browser edited this file")
            page.locator("#modal .close").click()
            expect(page.locator("#copy-asset")).to_have_count(0)
            expect(page.locator("#detail .dialog-heading #edit-asset")).to_be_visible()
            # An additional saved Task exercises bulk operations and deletion below.
            package(tmp_path / "managed/task-pool/browser-extra", "browser-extra")
            app.state.pools.sync()
            page.locator("#detail-close").click()
            page.locator("#tag-list input[value=review]").uncheck()
            expect(page.locator("#count")).to_have_text("3 条任务")
            page.locator("#check-all").check()
            expect(page.locator("#select-all")).to_have_count(0)
            page.locator("#clear-selection").click()
            expect(page.locator("#operations")).to_be_disabled()
            expect(page.locator("#rows input:checked")).to_have_count(0)
            page.locator("#check-all").check()
            page.locator("#add-tag").click()
            page.locator("#modal [name=tag]").fill("bulk")
            page.locator("#modal button[type=submit]").click()
            expect(page.locator("#modal")).not_to_be_visible()
            page.locator("#operations").click()
            page.locator("#batch-tags").click()
            page.locator('#modal input[value="bulk"]').check()
            page.locator("#modal button[type=submit]").click()
            expect(page.locator("#modal")).not_to_be_visible()
            expect(page.locator("#tag-list input[value=bulk]")).to_be_visible()
            # Pool tag color, rename, empty definitions, and individual removal.
            expect(page.locator("th").filter(has_text="状态")).to_have_count(0)
            expect(page.locator("#clear-tags")).to_have_count(0)
            page.locator('.tag-icon[data-tag="bulk"]').click()
            expect(page.locator(".color-swatch")).to_have_count(24)
            page.locator('[data-color="#dc2626"]').click()
            expect(page.locator('.tag-icon[data-tag="bulk"]')).to_have_css(
                "color", "rgb(220, 38, 38)"
            )
            page.locator('.tag-icon[data-tag="bulk"]').click()
            page.locator("[data-rename]").click()
            page.locator("#modal [name=tag]").fill("renamed-bulk")
            page.locator("#modal button[type=submit]").click()
            expect(page.locator("#modal")).not_to_be_visible()
            expect(page.locator('#rows .remove-tag[data-tag="renamed-bulk"]')).to_have_count(3)
            chip = page.locator('#rows .remove-tag[data-tag="renamed-bulk"]').first
            chip.locator("..").hover()
            chip.click()
            page.locator("#modal .cancel").click()
            expect(page.locator('#rows .remove-tag[data-tag="renamed-bulk"]')).to_have_count(3)
            chip.click()
            page.locator("#modal button[type=submit]").click()
            expect(page.locator("#modal")).not_to_be_visible()
            expect(page.locator('#rows .remove-tag[data-tag="renamed-bulk"]')).to_have_count(2)
            # Shift + wheel scrolls this cell, without scrolling the outer table.
            tag_cell = page.locator("#rows .table-tags").filter(has_text="json-tag")
            tag_cell.evaluate("el => el.style.width = '80px'")
            tag_cell.hover()
            page.keyboard.down("Shift")
            page.mouse.wheel(0, 150)
            page.keyboard.up("Shift")
            page.wait_for_function("el => el.scrollLeft > 0", arg=tag_cell.element_handle())
            assert page.locator(".table-wrap").evaluate("el => el.scrollLeft") == 0
            page.locator('.tag-icon[data-tag="renamed-bulk"]').click()
            page.screenshot(path="/tmp/envweaver-tag-menu-desktop.png", full_page=True)
            page.locator("[data-delete]").click()
            page.locator("#modal .cancel").click()
            expect(page.locator('.tag-icon[data-tag="renamed-bulk"]')).to_be_visible()
            page.locator('.tag-icon[data-tag="renamed-bulk"]').click()
            page.locator("[data-delete]").click()
            page.locator("#modal button[type=submit]").click()
            expect(page.locator("#modal")).not_to_be_visible()
            expect(page.locator('.tag-icon[data-tag="renamed-bulk"]')).to_have_count(0)
            expect(page.locator('#rows .remove-tag[data-tag="renamed-bulk"]')).to_have_count(0)
            page.locator("#query").fill("browser-extra")
            expect(page.locator("#count")).to_have_text("1 条任务")
            page.locator("#check-all").check()
            page.locator("#operations").click()
            expect(page.locator("#export")).to_have_count(0)
            expect(page.get_by_role("button", name="导入任务")).to_have_count(0)
            page.locator("#delete").click()
            page.locator("[name=physical]").check()
            page.locator("#modal button[type=submit]").click()
            expect(page.locator("#modal h2")).to_have_text("确认删除")
            page.locator("#modal button[type=submit]").click()
            expect(page.locator("#modal")).not_to_be_visible()
            page.locator("#query").fill("")
            page.locator("#refresh").click()
            expect(page.locator("#count")).to_have_text("2 条任务")
            page.locator("[data-kind=runtime]").click()
            expect(page.get_by_role("button", name="登记环境")).to_have_count(0)
            page.locator("#choose-pool").click()
            page.locator("#modal input[name=address]").fill(str(tmp_path / "managed/runtime-pool"))
            page.locator("#modal button[type=submit]").click()
            expect(page.locator("#modal")).not_to_be_visible()
            expect(page.locator("#count")).to_have_text("1 条环境")
            expect(page.locator('#tag-list input[value="benchmark"]')).to_have_count(0)
            page.locator("#add-tag").click()
            page.locator("#modal [name=tag]").fill("benchmark")
            page.locator("#modal button[type=submit]").click()
            expect(page.locator("#modal")).not_to_be_visible()
            page.locator('.tag-icon[data-tag="benchmark"]').click()
            page.locator('[data-color="#9333ea"]').click()
            expect(page.locator('.tag-icon[data-tag="benchmark"]')).to_have_css(
                "color", "rgb(147, 51, 234)"
            )
            page.locator("#check-all").check()
            page.locator("#operations").click()
            page.locator("#batch-tags").click()
            page.locator('#modal input[value="benchmark"]').check()
            page.locator("#modal button[type=submit]").click()
            expect(page.locator("#modal")).not_to_be_visible()
            page.locator("#rows .asset-name").click()
            expect(page.locator("#detail .chip").filter(has_text="benchmark")).to_have_css(
                "color", "rgb(147, 51, 234)"
            )
            page.locator("#detail [data-related]").first.click()
            expect(page.locator("#detail .chip").filter(has_text="benchmark")).to_have_css(
                "color", "rgb(37, 99, 235)"
            )
            page.locator("#detail-close").click()
            page.locator("#rows .asset-name").click()
            expect(page.locator("[data-related]")).to_have_count(4)
            page.screenshot(path="/tmp/envweaver-management-runtime-desktop.png", full_page=True)
            page.locator("#detail-close").click()
            page.locator("#check-all").check()
            page.locator("#operations").click()
            page.locator("#delete").click()
            page.locator("#modal button[type=submit]").click()
            expect(page.locator("#modal [name=ack]")).to_be_visible()
            confirm = page.locator("#modal button[type=submit]")
            ack = page.locator("#modal [name=ack]")
            expect(confirm).to_be_disabled()
            ack.check()
            expect(confirm).to_be_enabled()
            ack.uncheck()
            expect(confirm).to_be_disabled()
            gap = page.locator(".dependency-ack").evaluate(
                "el => el.getBoundingClientRect().top - el.previousElementSibling.getBoundingClientRect().bottom"
            )
            assert gap >= 14
            page.screenshot(path="/tmp/envweaver-delete-disabled-desktop.png", full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path="/tmp/envweaver-delete-disabled-mobile.png", full_page=True)
            page.set_viewport_size({"width": 1440, "height": 1000})
            # A failed request must keep the confirmation checkbox authoritative.
            page.route(
                "**/api/delete/confirm*",
                lambda route: route.fulfill(
                    status=409, content_type="application/json", body='{"detail":"确认已过期"}'
                ),
                times=1,
            )
            ack.check()
            confirm.click()
            expect(page.locator("#modal .form-error")).to_have_text("确认已过期")
            expect(confirm).to_be_enabled()
            ack.uncheck()
            expect(confirm).to_be_disabled()
            page.locator("#modal .cancel").click()
            page.locator("[data-kind=task]").click()
            expect(page.locator("#count")).to_have_text("2 条任务")
            page.screenshot(path="/tmp/envweaver-management-desktop.png", full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path="/tmp/envweaver-management-mobile.png", full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.locator("#rows .asset-name").first.click()
            page.locator("#detail details summary").click()
            expect(page.locator("#detail details pre")).to_be_visible()
            page.screenshot(path="/tmp/envweaver-management-mobile-detail.png", full_page=True)
            assert page.locator("#detail").bounding_box()["width"] <= 390
            page.locator("#detail-close").click()
            # Page-number navigation keeps first/last pages and nearby pages accessible.
            requested_pages = []

            def paginated(route):
                query = route.request.post_data_json
                requested_pages.append(query["page"])
                response = route.fetch()
                data = response.json()
                data["total"] = 0 if query["selection"]["query"] == "empty" else 600
                route.fulfill(response=response, json=data)

            page.route("**/api/assets/page*", paginated)
            page.locator("#refresh").click()
            expect(page.locator("#page-links button")).to_have_text(["1", "2", "3", "19", "20"])
            expect(page.locator("#page-links .page-gap")).to_have_count(1)
            page.locator('#page-links [data-page="20"]').click()
            expect(page.locator("#page-info")).to_have_text("第 20 / 20 页")
            expect(page.locator('#page-links [aria-current="page"]')).to_have_text("20")
            page.locator('#page-links [data-page="19"]').click()
            expect(page.locator("#page-info")).to_have_text("第 19 / 20 页")
            page.locator('#page-links [data-page="1"]').click()
            expect(page.locator("#page-info")).to_have_text("第 1 / 20 页")
            transitions = [
                page
                for i, page in enumerate(requested_pages)
                if i == 0 or page != requested_pages[i - 1]
            ]
            assert transitions[-3:] == [20, 19, 1]
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path="/tmp/envweaver-pagination-mobile.png", full_page=True)
            page.locator("#query").fill("empty")
            expect(page.locator("#count")).to_have_text("0 条任务")
            expect(page.locator("#page-links button")).to_have_text(["1"])
            assert errors == [], errors
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
