from io import StringIO

from rich.console import Console

from envweaver.evaluation.runner import _Panel


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
