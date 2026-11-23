import json
import os
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from envweaver import TrajPool
from envweaver.management.api import create_app
from envweaver.management.models import PoolInput
from envweaver.trajectory.atif import add_step, document, validate_trajectory
from tests.trajectory.test_pool import rollout

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("ENVWEAVER_TRAJ_BROWSER") != "1", reason="opt-in browser"),
]


def nested_trajectories(source):
    path = source / "agent/trajectory.json"
    root = json.loads(path.read_text())
    metadata = {"agent": "worker", "model": "test-model"}
    child = document(metadata, "embedded-a")
    child["session_id"] = "shared-session"
    add_step(
        child,
        "user",
        [
            {"type": "text", "text": "before image"},
            root["steps"][0]["message"][1],
            {"type": "text", "text": "after image"},
        ],
    )
    add_step(
        child,
        "agent",
        "child answer",
        reasoning_content="visible reasoning",
        tool_calls=[
            {"tool_call_id": "call-1", "function_name": "shell", "arguments": {"command": "pwd"}}
        ],
        observation={
            "results": [
                {
                    "source_call_id": "call-1",
                    "content": "/workspace",
                    "subagent_trajectory_ref": [{"trajectory_id": "embedded-b"}],
                }
            ]
        },
        extra={"custom_field": "preserved"},
        is_copied_context=True,
    )
    grandchild = document({"agent": "grandchild"}, "embedded-b")
    add_step(grandchild, "agent", "nested child, not root sibling")
    child["subagent_trajectories"] = [grandchild]
    sibling = document({"agent": "sibling"}, "embedded-b")
    sibling["session_id"] = "shared-session"
    add_step(sibling, "agent", "root sibling answer")
    root["steps"][-1]["message"] = "\n\n" + root["steps"][-1]["message"]
    root["final_metrics"] = {"total_steps": 2, "total_cost_usd": 0}
    root["subagent_trajectories"] = [child, sibling]
    root["steps"][-1]["observation"] = {
        "results": [
            {
                "content": "delegated",
                "subagent_trajectory_ref": [
                    {"trajectory_id": "embedded-a", "session_id": "shared-session"},
                    {"trajectory_path": "trajectory.external.json"},
                ],
            }
        ]
    }
    external = document({"agent": "external"}, "external")
    add_step(external, "agent", "external child answer")
    external["continued_trajectory_ref"] = "trajectory.json"
    external["extra"] = {"custom_root": "kept"}
    for file, value in [(path, root), (source / "agent/trajectory.external.json", external)]:
        validate_trajectory(value)
        file.write_text(json.dumps(value))


def test_trajectory_browser(tmp_path):
    pool = TrajPool(tmp_path / "pool", completed_only=False)
    for number, score in enumerate([0, 1, None]):
        source = tmp_path / f"source-{number}"
        rollout(source, score=score, reason="error" if score is None else "completed")
        if score == 0:
            value = json.loads((source / "agent/trajectory.json").read_text())
            value["steps"][-1]["metrics"] = {"prompt_tokens": 1234, "completion_tokens": 0}
            (source / "agent/trajectory.json").write_text(json.dumps(value))
        if score == 1:
            result = json.loads((source / "result.json").read_text())
            result["agent_stop_reason"] = "timeout"
            result["errors"] = [
                {"stage": "agent", "type": "timeout", "message": "test time limit <script>"}
            ]
            (source / "result.json").write_text(json.dumps(result))
            value = json.loads((source / "agent/trajectory.json").read_text())
            value["steps"][-1]["metrics"] = {"prompt_tokens": 1234, "completion_tokens": 0}
            (source / "agent/trajectory.json").write_text(json.dumps(value))
        if score == 1:
            nested_trajectories(source)
        pool.save(output_dir=source)
    app = create_app()
    app.state.pools.add(PoolInput(kind="trajectory", address=str(pool.root)))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.02)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel="chromium", headless=True, args=["--no-sandbox"]
            )
            page = browser.new_page(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
            page.add_init_script("""
                window.groupPolls = 0;
                let pendingGroup = false, delayed = false;
                const originalFetch = window.fetch;
                window.fetch = async (...args) => {
                    const path = new URL(args[0], location.origin).pathname;
                    if (path.endsWith('/index') && pendingGroup) window.groupPolls++;
                    const group = path.endsWith('/assets/page') &&
                        JSON.parse(args[1].body).selection.task_group;
                    if (!group || delayed) return originalFetch(...args);
                    delayed = pendingGroup = true;
                    try {
                        const response = await originalFetch(...args);
                        await new Promise(resolve => setTimeout(resolve, 4500));
                        return response;
                    } finally { pendingGroup = false; }
                };
            """)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{port}")
            page.get_by_role("button", name="Traj Pool", exact=True).click()
            expect(page.locator("#count")).to_have_text("3 条轨迹")
            expect(page.locator(".task-group-toggle")).to_have_count(1)
            expect(page.locator("#rows [data-open]")).to_have_count(0)
            page.locator("[data-group-select]").check()
            expect(page.locator("#selected-label")).to_have_text(" · 已选 3 条")
            expect(page.locator("#check-all")).to_be_checked()
            expect(page.locator("#rows [data-open]")).to_have_count(0)
            expect(page.locator(".task-group-toggle")).to_have_attribute("aria-expanded", "false")
            page.locator("#clear-selection").click()
            page.locator(".task-group-toggle").click()
            expect(page.locator("#rows [data-open]")).to_have_count(3, timeout=10000)
            assert page.evaluate("window.groupPolls") == 0
            assert (
                "1.23K" in page.locator('#rows td[data-column="input_tokens"]').all_text_contents()
            )
            assert "0" in page.locator('#rows td[data-column="output_tokens"]').all_text_contents()
            page.locator(".task-group-toggle").click()
            expect(page.locator("#rows [data-open]").first).to_be_hidden()
            expect(page.locator("#backend-filter")).to_be_hidden()
            page.locator("#model-filter").click()
            expect(page.get_by_text("test-model", exact=True).last).to_be_visible()
            page.keyboard.press("Escape")
            page.get_by_role("button", name="更多筛选").click()
            page.locator("#score-min").fill("1")
            page.get_by_role("button", name="应用筛选").click()
            expect(page.locator("#count")).to_have_text("1 条轨迹")
            page.locator("[data-group-select]").check()
            expect(page.locator("#selected-label")).to_have_text(" · 已选 1 条")
            page.locator("#clear-selection").click()
            page.locator(".task-group-toggle").click()
            expect(page.locator("#rows [data-open]")).to_have_count(1)
            expect(page.locator("thead th")).to_have_count(11)
            expect(page.locator('#rows td[data-column="input_tokens"]')).to_have_text("—")
            expect(page.locator('#rows td[data-column="output_tokens"]')).to_have_text("—")
            expect(page.locator('#rows td[data-column="agent_stop_reason"]')).to_have_text(
                "timeout"
            )
            page.locator("#rows [data-errors]").click()
            expect(page.locator("#modal")).to_contain_text("agent · timeout")
            expect(page.locator("#modal")).to_contain_text("test time limit <script>")
            page.screenshot(path="/tmp/envweaver-traj-errors.png", full_page=True)
            page.keyboard.press("Escape")
            page.screenshot(path="/tmp/envweaver-traj-list.png", full_page=True)
            page.locator("#rows [data-open]").first.click()
            expect(page.locator("#detail h3").filter(has_text="关联 Task")).to_have_count(0)
            expect(page.locator("#detail .trajectory-facts dt").filter(has_text="关联 Task")).to_have_count(1)
            expect(page.locator(".verification-result")).to_have_count(0)
            expect(page.locator("#asset-json")).to_have_count(0)
            expect(page.locator("#detail #goal")).to_have_count(0)
            expect(page.locator("#detail .detail-content > section[hidden]")).to_have_count(0)
            expect(page.locator("#trajectory-content .trajectory-step")).to_have_count(3)
            expect(page.locator("#trajectory-content")).not_to_contain_text("ATIF-v1.8")
            expect(page.locator("#trajectory-metrics")).to_contain_text('"total_steps": 2')
            expect(page.locator("#trajectory-metrics")).to_contain_text('"total_cost_usd": 0')
            expect(page.locator("#trajectory-content summary").filter(has_text="汇总指标")).to_have_count(0)
            expect(page.locator("#trajectory-content").get_by_text("补充字段", exact=True)).to_have_count(0)
            expect(page.locator("#files")).to_contain_text("trajectory.child.json")
            page.locator('#files [data-file="result.json"]').click()
            expect(page.locator("#modal pre code")).to_contain_text('"verification_result"')
            expect(page.locator("#modal pre code")).to_contain_text('"errors"')
            page.keyboard.press("Escape")
            expect(page.locator("#trajectory-content")).to_contain_text("done")
            assert page.locator("#trajectory-content .trajectory-step").nth(1).locator(".trajectory-text").first.text_content() == "done"
            page.screenshot(path="/tmp/envweaver-traj-detail.png", full_page=True)
            page.locator("#trajectory-content .trajectory-image").click()
            expect(page.locator(".image-viewer")).to_be_visible()
            assert page.locator(".image-viewer img").evaluate("image => image.naturalWidth") == 1
            page.keyboard.press("Escape")
            expect(page.locator('#trajectory-content [data-traj-file*="files/"]')).to_have_count(0)
            expect(page.locator("#trajectory-content .trace-card")).to_have_count(4)
            page.locator("#trajectory-content").get_by_role(
                "button", name="子 Agent · worker", exact=True
            ).first.click()
            child = page.locator(".trajectory-viewer").last
            expect(child).to_be_visible()
            expect(child.locator(".trajectory-text").first).to_have_text("before image")
            expect(child.locator(".trajectory-text").nth(1)).to_have_text("after image")
            expect(child.locator(".trajectory-image img")).to_be_visible()
            assert (
                child.locator(".trajectory-image img").evaluate("image => image.naturalWidth") == 1
            )
            child.locator(".trajectory-image").click()
            expect(page.locator(".image-viewer")).to_be_visible()
            page.keyboard.press("Escape")
            expect(child).to_contain_text("visible reasoning")
            expect(child).to_contain_text("shell")
            expect(child).to_contain_text("/workspace")
            expect(child.get_by_text("补充字段", exact=True)).to_have_count(0)
            page.screenshot(path="/tmp/envweaver-subtrajectory-wide.png", full_page=True)
            child.get_by_role(
                "button", name="子 Agent · grandchild", exact=True
            ).last.click()
            expect(page.locator(".trajectory-viewer")).to_have_count(2)
            expect(page.locator(".trajectory-viewer").last).to_contain_text(
                "nested child, not root sibling"
            )
            page.keyboard.press("Escape")
            expect(page.locator(".trajectory-viewer")).to_have_count(1)
            expect(child).to_contain_text("child answer")
            page.set_viewport_size({"width": 480, "height": 850})
            page.screenshot(path="/tmp/envweaver-subtrajectory-narrow.png", full_page=True)
            page.keyboard.press("Escape")
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.get_by_text("未记录调用位置的子 Agent", exact=True).click()
            page.locator("#trajectory-content").get_by_role(
                "button", name="子 Agent · sibling", exact=True
            ).click()
            expect(page.locator(".trajectory-viewer")).to_contain_text("root sibling answer")
            page.keyboard.press("Escape")
            # External child opens a rendered window; continuations append in the same reader.
            page.locator('#trajectory-content [data-traj-file="agent/trajectory.external.json"]').click()
            expect(page.locator(".trajectory-viewer")).to_contain_text("external child answer")
            expect(page.locator(".trajectory-viewer .trajectory-step")).to_have_count(4)
            expect(page.locator(".trajectory-viewer").get_by_text("补充字段", exact=True)).to_have_count(0)
            page.keyboard.press("Escape")
            expect(page.locator("#trajectory-content .trajectory-step").last).to_contain_text("child")
            expect(page.locator('#trajectory-content [data-traj-file="agent/trajectory.child.json"]')).to_have_count(0)
            # Artifact files display the original text; trajectory references still render ATIF.
            page.locator('#files [data-file="agent/trajectory.external.json"]').click()
            expect(page.locator("#modal pre code")).to_be_visible()
            assert page.locator("#modal pre code").text_content() == (tmp_path / "source-1/agent/trajectory.external.json").read_text()
            expect(page.locator(".trajectory-viewer")).to_have_count(0)
            page.keyboard.press("Escape")
            # Failed reference requests are retryable within the same window.
            pattern = "**/api/trajectories/*/file?path=agent%2Ftrajectory.external.json*"
            page.route(
                pattern,
                lambda route: route.fulfill(status=503, json={"detail": "reference unavailable"}),
            )
            reference = page.locator(
                '#trajectory-content [data-traj-file="agent/trajectory.external.json"]'
            )
            reference.click()
            expect(page.locator(".trajectory-viewer [role=alert]")).to_have_text(
                "reference unavailable"
            )
            page.unroute(pattern)
            page.locator(".trajectory-viewer").get_by_role(
                "button", name="重试", exact=True
            ).click()
            expect(page.locator(".trajectory-viewer .trajectory-step")).to_have_count(4)
            page.keyboard.press("Escape")
            # Invalid JSON is reported; closing a pending load must not resurrect its dialog.
            page.route(pattern, lambda route: route.fulfill(status=200, body="not JSON"))
            reference.click()
            expect(page.locator(".trajectory-viewer [role=alert]")).to_be_visible()
            page.keyboard.press("Escape")
            page.unroute(pattern)
            pending = []
            page.route(pattern, lambda route: pending.append(route))
            reference.click()
            expect(page.locator(".trajectory-viewer")).to_contain_text("加载中")
            page.keyboard.press("Escape")
            expect(page.locator(".trajectory-viewer")).to_have_count(0)
            for route in pending:
                route.abort()
            page.unroute(pattern)
            expect(page.locator("#edit-asset")).to_have_count(0)
            page.set_viewport_size({"width": 480, "height": 850})
            page.screenshot(path="/tmp/envweaver-traj-narrow.png", full_page=True)
            expect(page.locator("#detail-close")).to_be_visible()
            page.locator("#detail-close").click()
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.locator("#check-all").check()
            page.locator("#operations").click()
            with page.expect_download() as download:
                page.get_by_role("button", name="导出", exact=True).click()
            assert download.value.suggested_filename == "trajectories.zip"
            # A single Task can span several child pages without being split into groups.
            page.locator("#clear-selection").click()
            page.locator("#score-min").fill("")
            page.get_by_role("button", name="应用筛选").click()
            for number in range(28):
                source = tmp_path / f"many-{number}"
                rollout(source)
                pool.save(output_dir=source)
            source = tmp_path / "other-task"
            rollout(source, task_ref="/other/example/task.json")
            pool.save(output_dir=source)
            page.locator("#refresh").click()
            expect(page.locator("#count")).to_have_text("32 条轨迹")
            expect(page.locator(".task-group-toggle")).to_have_count(2)
            failed = False

            def fail_first_group_request(route):
                nonlocal failed
                selection = route.request.post_data_json.get("selection", {})
                if selection.get("task_group") and not failed:
                    failed = True
                    route.fulfill(status=503, json={"detail": "temporary test failure"})
                else:
                    route.continue_()

            page.route("**/api/assets/page*", fail_first_group_request)
            group = page.locator('.task-group-toggle[title="/tasks/example/task.json"]')
            group_check = page.locator('[data-group-select="ref:/tasks/example/task.json"]')
            group_check.check()
            expect(page.locator("#selected-label")).to_have_text(" · 已选 31 条")
            group.click()
            expect(page.get_by_text("temporary test failure", exact=True)).to_be_visible()
            page.get_by_role("button", name="重试", exact=True).click()
            expect(page.locator("#rows [data-open]")).to_have_count(30)
            expect(page.locator("#rows input[data-id]:checked")).to_have_count(30)
            page.locator("#rows input[data-id]").first.uncheck()
            expect(page.locator("#selected-label")).to_have_text(" · 已选 30 条")
            expect(group_check).to_have_js_property("indeterminate", True)
            page.unroute("**/api/assets/page*", fail_first_group_request)
            page.get_by_role("button", name="加载更多（30 / 31）").click()
            expect(page.locator("#rows [data-open]")).to_have_count(31)
            expect(page.locator("#rows input[data-id]").last).to_be_checked()
            group_check.click()
            expect(page.locator("#selected-label")).to_have_text(" · 已选 31 条")
            group_check.uncheck()
            expect(page.locator("#selected-label")).to_have_text("")
            expect(page.locator("#rows input[data-id]:checked")).to_have_count(0)
            page.locator("#rows input[data-id]").first.check()
            expect(page.locator("#selected-label")).to_have_text(" · 已选 1 条")
            group.click()
            expect(page.locator("#rows [data-open]").first).to_be_hidden()
            expect(page.locator("#selected-label")).to_have_text(" · 已选 1 条")
            group.click()
            expect(page.locator("#rows input[data-id]").first).to_be_checked()
            page.locator("#check-all").check()
            expect(page.locator("#selected-label")).to_have_text(" · 已选 32 条")
            group_check.uncheck()
            expect(page.locator("#selected-label")).to_have_text(" · 已选 1 条")
            page.locator("#rows input[data-id]").first.check()
            expect(page.locator("#selected-label")).to_have_text(" · 已选 2 条")
            expect(group_check).to_have_js_property("indeterminate", True)
            group_check.click()
            expect(page.locator("#selected-label")).to_have_text(" · 已选 32 条")
            expect(page.locator("#check-all")).to_be_checked()
            page.screenshot(path="/tmp/envweaver-traj-groups-wide.png", full_page=True)
            page.get_by_role("button", name="更多筛选").click()
            page.set_viewport_size({"width": 480, "height": 850})
            page.screenshot(path="/tmp/envweaver-traj-groups-narrow.png", full_page=True)
            page.reload()
            expect(page.locator("#page-title")).to_have_text("Traj Pool")
            expect(page.locator(".task-group-toggle")).to_have_count(2)
            expect(page.locator("#rows [data-open]")).to_have_count(0)
            page.get_by_role("button", name="Task Pool", exact=True).click()
            expect(page.locator("thead th")).to_have_count(8)
            page.get_by_role("button", name="Traj Pool", exact=True).click()
            expect(page.locator("thead th")).to_have_count(11)
            # Reader-only fixtures replace the document; isolate them from the
            # management page's in-flight requests and background refresh timer.
            page.route("**/reader-fixture", lambda route: route.fulfill(
                content_type="text/html",
                body='<!doctype html><link rel="stylesheet" href="/src/style.css"><body></body>',
            ))
            page.goto(f"http://127.0.0.1:{port}/reader-fixture")
            check_reader_edges(page)
            check_reader_scrolling(page)
            assert not errors, errors
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def check_reader_edges(page):
    """Exercise the reader with recorded compaction, continuation and long content."""
    root = document({"agent": "fixture"}, "reader-root")
    add_step(root, "system", "", extra={"pi_compaction": {}})
    add_step(root, "system", "", extra={"pi_compaction": {"compactionEntry": {"summary": "summary <script> kept as text"}}})
    add_step(root, "system", "compacted", extra={"context_management": {"type": "compaction"}}, observation={"results": [{"subagent_trajectory_ref": [{"trajectory_path": "summary.json"}]}]})
    for text in ["after encrypted compaction", "same context"]:
        add_step(root, "agent", text, extra={"context": {"missing": ["encrypted_compaction"], "path": "files/private-context.json"}})
    add_step(root, "user", "long content\n" * 300)
    root["continued_trajectory_ref"] = "next.json"
    child = document({"agent": "summarizer"}, "summary")
    add_step(child, "agent", "recorded summary process")
    following = document({"agent": "fixture"}, "reader-next")
    add_step(following, "user", "copied context must not repeat", is_copied_context=True)
    add_step(following, "agent", "continued execution")
    following["continued_trajectory_ref"] = "./trajectory.json"
    files = {"agent/trajectory.json": root, "agent/next.json": following, "agent/summary.json": child}
    fail_next = True

    def serve_file(route):
        nonlocal fail_next
        path = parse_qs(urlsplit(route.request.url).query)["path"][0]
        if path == "agent/next.json" and fail_next:
            fail_next = False
            route.fulfill(status=503, json={"detail": "continuation unavailable"})
        else:
            route.fulfill(json=files[path])

    page.route("**/api/trajectories/reader-test/file?*", serve_file)
    page.set_viewport_size({"width": 1100, "height": 950})
    page.evaluate("""async () => {
        document.body.innerHTML = '<div id="reader-fixture" style="padding:20px"><div id="trajectory-metrics"></div><div id="trajectory-content"></div></div><dialog id="modal"></dialog><div id="toast"></div>';
        window.readerAbort = new AbortController();
        const {loadTrajectory} = await import('/src/pages/trajectories.js');
        await loadTrajectory({id:'reader-test', pool_id:'fixture', spec:{agent_result:{artifacts:{trajectory:'trajectory.json'}}}}, document.querySelector('#reader-fixture'), readerAbort.signal);
    }""")
    reader = page.locator("#trajectory-content")
    expect(reader.locator(".trajectory-step")).to_have_count(6)
    expect(reader.get_by_text("上下文压缩", exact=True)).to_have_count(4)
    expect(reader.locator('[data-traj-file*="private-context"]')).to_have_count(0)
    expect(reader.locator(".trajectory-step").first.get_by_role("button")).to_have_count(0)
    reader.get_by_role("button", name="查看摘要").click()
    expect(page.locator("#modal .trajectory-text")).to_have_text("summary <script> kept as text")
    expect(page.locator("#modal script")).to_have_count(0)
    page.keyboard.press("Escape")
    reader.get_by_role("button", name="压缩过程 1").click()
    expect(page.locator(".trajectory-viewer")).to_contain_text("recorded summary process")
    page.keyboard.press("Escape")
    assert reader.evaluate("el => el.scrollHeight > el.clientHeight && el.clientHeight <= innerHeight * .75 + 1")
    long_body = reader.locator(".trace-card-body").last
    assert long_body.evaluate("el => el.scrollHeight > el.clientHeight && el.clientHeight <= 280")
    reader.evaluate("el => el.scrollTop = el.scrollHeight")
    expect(reader.get_by_role("alert")).to_have_text("continuation unavailable")
    reader.get_by_role("button", name="重试").click()
    expect(reader).to_contain_text("continued execution")
    expect(reader).not_to_contain_text("copied context must not repeat")
    expect(reader).to_contain_text("续接轨迹存在循环引用")
    expect(reader.locator(".trajectory-step")).to_have_count(7)
    expect(reader.locator(".trajectory-step").last.locator(".trace-card-heading")).to_contain_text("#7")
    reader.evaluate("el => el.scrollTop = 0")
    page.screenshot(path="/tmp/envweaver-reader-compaction.png", full_page=True)
    page.set_viewport_size({"width": 480, "height": 850})
    assert reader.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    page.screenshot(path="/tmp/envweaver-reader-compaction-narrow.png", full_page=True)
    page.evaluate("readerAbort.abort()")
    page.unroute("**/api/trajectories/reader-test/file?*", serve_file)


def check_reader_scrolling(page):
    page.set_viewport_size({"width": 1100, "height": 950})
    page.evaluate("""async () => {
        document.body.innerHTML = '<div id="scroll-reader" class="trajectory-reader" style="margin:20px"></div>';
        const {renderTrajectory} = await import('/src/components/trajectory-view.js');
        const steps = Array.from({length: 4}, (_, i) => ({step_id:i+1, source:'agent', message:'long message\\n'.repeat(100)}));
        renderTrajectory(document.querySelector('#scroll-reader'), {schema_version:'ATIF-v1.8', agent:{name:'fixture'}, steps}, {metricsTarget: document.createElement('div')});
    }""")
    reader = page.locator("#scroll-reader")
    body = reader.locator(".trace-card-body").first
    body.hover()
    page.mouse.wheel(0, 120)
    page.wait_for_function("document.querySelector('.trace-card-body').scrollTop > 0")
    assert reader.evaluate("el => el.scrollTop") == 0
    # At the bottom of a message, wheel events continue into the reader.
    body.evaluate("el => el.scrollTop = el.scrollHeight")
    page.mouse.wheel(0, 100)
    page.wait_for_function("document.querySelector('#scroll-reader').scrollTop > 0")
    page.wait_for_timeout(200)  # Let the native wheel animation finish before reversing.
    before = reader.evaluate("el => el.scrollTop")
    body.evaluate("el => el.scrollTop = 0")
    bounds = reader.bounding_box()
    page.mouse.move(bounds['x'] + 60, bounds['y'] + 100)
    page.mouse.wheel(0, -100)
    page.wait_for_function("before => document.querySelector('#scroll-reader').scrollTop < before", arg=before)
    page.wait_for_timeout(200)
    # The side gutter always scrolls the timeline, even when the message can scroll.
    reader.evaluate("el => el.scrollTop = 0")
    body.evaluate("el => el.scrollTop = 100")
    page.mouse.move(bounds['x'] + 10, bounds['y'] + 120)
    page.mouse.wheel(0, 150)
    page.wait_for_function("document.querySelector('#scroll-reader').scrollTop > 0")
    assert body.evaluate("el => el.scrollTop") == 100
    page.screenshot(path="/tmp/envweaver-reader-scroll-wide.png", full_page=True)
    page.set_viewport_size({"width": 480, "height": 850})
    assert reader.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    page.screenshot(path="/tmp/envweaver-reader-scroll-narrow.png", full_page=True)

    # The next scroll boundary hands off to the enclosing detail dialog too.
    page.set_viewport_size({"width": 1100, "height": 950})
    page.evaluate("""() => {
        const dialog = document.createElement('dialog');
        dialog.id = 'scroll-detail';
        dialog.className = 'detail-panel';
        dialog.innerHTML = '<div style="height:400px">详情属性</div><div id="reader-slot"></div><div style="height:500px">产物文件</div>';
        dialog.querySelector('#reader-slot').append(document.querySelector('#scroll-reader'));
        document.body.append(dialog);
        dialog.showModal();
        dialog.scrollTop = 250;
        document.querySelector('#scroll-reader').scrollTop = 0;
    }""")
    detail = page.locator('#scroll-detail')
    bounds = reader.bounding_box()
    page.mouse.move(bounds['x'] + 10, bounds['y'] + 80)
    before = detail.evaluate('el => el.scrollTop')
    page.mouse.wheel(0, -100)
    page.wait_for_function("before => document.querySelector('#scroll-detail').scrollTop < before", arg=before)
    page.wait_for_timeout(200)
    assert reader.evaluate('el => el.scrollTop') == 0
    detail.evaluate('el => el.scrollTop = 250')
    reader.evaluate('el => el.scrollTop = el.scrollHeight')
    bounds = reader.bounding_box()
    page.mouse.move(bounds['x'] + 10, bounds['y'] + 80)
    before = detail.evaluate('el => el.scrollTop')
    page.mouse.wheel(0, 100)
    page.wait_for_function("before => document.querySelector('#scroll-detail').scrollTop > before", arg=before)
    page.screenshot(path='/tmp/envweaver-reader-outer-scroll.png', full_page=True)
    detail.evaluate('el => el.close()')
