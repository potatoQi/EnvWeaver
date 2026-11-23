"""Live progress display for Eval."""

import math
import threading
import time

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from ..errors import AgentStopReason, ErrorType


class _Panel:
    def __init__(self, total=None):
        self.lock = threading.RLock()
        self.rows = {}
        self.total = total
        self.finished = {}
        self.started = time.monotonic()
        self.stopped = None
        self.live = Live(
            get_renderable=self._render, console=Console(stderr=True), refresh_per_second=4
        )

    def __enter__(self):
        self.live.start()
        return self

    def __exit__(self, *args):
        with self.lock:
            self.stopped = time.monotonic()
        self.live.stop()

    def _render(self):
        with self.lock:
            completed = len(self.finished)
            filled = int(12 * completed / self.total) if self.total else 0
            bar = "█" * filled + "░" * (12 - filled)
            scores = [score for score in self.finished.values() if score is not None]
            mean = f"{sum(scores) / len(scores):.3f}".rstrip("0").rstrip(".") if scores else "—"
            running = sum(key not in self.finished for key in self.rows) if self.stopped is None else 0
            seconds = int((self.stopped if self.stopped is not None else time.monotonic()) - self.started)
            if seconds >= 3600:
                elapsed = f"{seconds // 3600}h {seconds % 3600 // 60}m"
            elif seconds >= 60:
                elapsed = f"{seconds // 60}m"
            else:
                elapsed = f"{seconds}s"
            total = self.total if self.total is not None else "?"
            summary = Text(
                f"评测进度  {completed}/{total}  {bar} · 平均 score {mean}"
                f" · 运行 {running} · 已用时 {elapsed}"
            )
            return Group(summary, self._table())

    def finish(self, key, result):
        """Count a final result once; exclude cancellation and invalid scores."""
        if result.get("agent_stop_reason") == AgentStopReason.CANCELLED or any(
            error["type"] == ErrorType.CANCELLED for error in result.get("errors", [])
        ):
            return
        score = (result.get("verification_result") or {}).get("score")
        valid = result.get("score_valid") and type(score) in (int, float) and math.isfinite(score)
        with self.lock:
            self.finished[key] = score if valid else None

    def _table(self):
        table = Table(title="Eval", expand=True)
        table.add_column("Task", ratio=1, no_wrap=True, overflow="ellipsis")
        table.add_column("Stage", width=13)
        table.add_column("Score", width=5)
        table.add_column("Error", ratio=1, no_wrap=True, overflow="ellipsis")
        rows = sorted(
            reversed(self.rows.values()), key=lambda row: row[1] in {"done", "failed", "resumed"}
        )
        for name, stage, score, error in rows:
            table.add_row(Text(name), Text(stage), Text(score), Text(" ".join(error.split())))
        return table

    def update(self, key, name, stage, result=None):
        with self.lock:
            verification = (result or {}).get("verification_result")
            score = str(verification["score"]) if verification else "—"
            errors = result["errors"] if result else []
            # Reinsert so dictionary order tracks the latest update, including retries.
            self.rows.pop(key, None)
            self.rows[key] = (name, stage, score, errors[-1]["message"] if errors else "")
