from io import StringIO

from rich.console import Console

from envweaver import AgentOutput
from envweaver.evaluation.panel import _Panel
from envweaver.trajectory.atif import add_step, document


def render(panel):
    stream = StringIO()
    Console(file=stream, width=100, color_system=None).print(panel._table())
    return stream.getvalue()


def test_active_tasks_first_with_latest_updates_first():
    panel = _Panel()
    panel.update(0, "first", "agent")
    panel.update(1, "second", "setup")
    panel.update(2, "completed", "done")
    panel.update(3, "resumed", "resumed")
    panel.update(4, "failed", "failed")
    panel.update(0, "first", "verify")

    text = render(panel)
    names = ("first", "second", "failed", "resumed", "completed")
    assert [text.index(name) for name in names] == sorted(text.index(name) for name in names)

    panel.update(4, "failed", "retry")
    assert render(panel).index("failed") < render(panel).index("first")


def test_error_is_one_line_without_changing_result():
    panel = _Panel()
    message = "Transport failed\n" + "details " * 100 + "END"
    result = {"errors": [{"message": message}]}
    panel.update(0, "task", "failed", result)

    text = render(panel)
    assert "task" in text and "failed" in text
    assert "Transport failed" in text
    assert "…" in text
    assert "END" not in text
    assert len(text.splitlines()) == 6
    assert result["errors"][0]["message"] == message


def test_agent_steps_refresh_per_attempt(tmp_path):
    metadata = {"agent": "example", "model": None, "agent_version": None}
    panel = _Panel()
    output = AgentOutput(tmp_path / "first", metadata)
    panel.update(0, "first", "agent", output=output)
    assert "step:" not in render(panel)
    initial = document(metadata, "first")
    add_step(initial, "agent", "first answer")
    output.save_trajectory(initial)
    assert "agent (step: 1)" in render(panel)
    panel.update(0, "first", "verify")
    assert "step:" not in render(panel)
    retry = AgentOutput(tmp_path / "retry", metadata)
    panel.update(0, "first", "agent", output=retry)
    add_step(initial, "agent", "late answer")
    output.save_trajectory(initial)
    assert "step:" not in render(panel)
    root = document(metadata, "main")
    add_step(root, "user", "task")
    add_step(root, "agent", "first answer")
    retry.save_trajectory(root)
    retry.save_trajectory(root)
    assert "agent (step: 1)" in render(panel)
    continuation = document(metadata, "continued")
    root["continued_trajectory_ref"] = "trajectory.next.json"
    add_step(continuation, "agent", "first answer", is_copied_context=True)
    add_step(continuation, "agent", "second answer")
    child = document(metadata, "child")
    add_step(child, "agent", "child answer")
    retry.save_trajectory(root, related={"trajectory.next.json": continuation, "trajectory.child.json": child})
    assert "agent (step: 2)" in render(panel)
    add_step(continuation, "agent", "third answer")
    retry.save_trajectory(root, related={"trajectory.next.json": continuation})
    assert "agent (step: 3)" in render(panel)
    continuation["steps"][-1]["is_copied_context"] = True
    retry.save_trajectory(root, related={"trajectory.next.json": continuation})
    assert "agent (step: 2)" in render(panel)
