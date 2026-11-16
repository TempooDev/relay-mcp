"""Read-only viewer for the task database: `relay-mcp tasks | show | watch`.

It reads the SQLite file directly, so it works from any terminal, in parallel with the
server, and even when the server is stopped. It never writes.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

TERMINAL_STATES = ("completed", "failed", "cancelled", "interrupted")
COLORS = {
    "completed": "32", "failed": "31", "cancelled": "33", "interrupted": "33",
    "running": "36", "pending": "36",
}  # fmt: skip
RECENT_WINDOW = 200  # how many recent tasks `watch` keeps an eye on


class ViewerError(RuntimeError):
    """A problem to report to the user (missing database, unknown task...)."""


class Style:
    def __init__(self, color: bool):
        self.color = color

    def paint(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def status(self, status: str) -> str:
        return self.paint(status.ljust(11), COLORS.get(status, "0"))

    def dim(self, text: str) -> str:
        return self.paint(text, "2")

    def bold(self, text: str) -> str:
        return self.paint(text, "1")


def auto_style(out: TextIO) -> Style:
    color = out.isatty() and "NO_COLOR" not in os.environ
    if color and os.name == "nt":
        os.system("")  # enables ANSI escape sequences in the legacy Windows console
    return Style(color)


def connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise ViewerError(f"No task database at {path}. Has the server run yet?")
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        db.execute("SELECT 1 FROM tasks LIMIT 1")
    except sqlite3.OperationalError:
        try:  # read-only WAL access can fail if the -shm file is missing; plain open still reads
            db = sqlite3.connect(path)
            db.execute("SELECT 1 FROM tasks LIMIT 1")
        except sqlite3.OperationalError as e:
            raise ViewerError(f"{path} is not a relay-mcp task database ({e})") from e
    db.row_factory = sqlite3.Row
    return db


def fetch_recent(db: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = db.execute("SELECT * FROM tasks ORDER BY created DESC, rowid DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows.fetchall()]


def find_task(db: sqlite3.Connection, ident: str) -> dict[str, Any]:
    rows = db.execute("SELECT * FROM tasks WHERE id LIKE ? ESCAPE '\\' LIMIT 2", (_prefix(ident),))
    found = [dict(r) for r in rows.fetchall()]
    if not found:
        raise ViewerError(f"No task starts with '{ident}'")
    if len(found) > 1:
        raise ViewerError(f"'{ident}' matches more than one task; type more characters")
    return found[0]


def _prefix(ident: str) -> str:
    return ident.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ------------------------------------------------------------------ formatting


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def duration(task: dict[str, Any], now: datetime | None = None) -> str:
    start = _parse(task.get("created"))
    if start is None:
        return "-"
    end = _parse(task.get("finished")) or (now or datetime.now())
    seconds = max(0, int((end - start).total_seconds()))
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m{seconds % 60:02d}s"


def clock(task: dict[str, Any]) -> str:
    created = _parse(task.get("created"))
    return created.strftime("%H:%M:%S") if created else "--:--:--"


def route(task: dict[str, Any]) -> str:
    tier, agent = task.get("tier") or "-", task.get("agent")
    label = f"{agent}@{tier}" if agent and tier != "-" else (agent or tier)
    if task.get("thread"):
        label += f" #{task['thread']}"
    model = task.get("model")
    return f"{label} -> {model}" if model else label


def one_line(task: dict[str, Any], style: Style, width: int = 60) -> str:
    desc = " ".join((task.get("description") or "").split())
    if len(desc) > width:
        desc = desc[: width - 3] + "..."
    return (
        f"{style.dim(clock(task))}  {style.bold(task['id'])}  {style.status(task['status'])} "
        f"{route(task):<42} {duration(task):>7}  {desc}"
    )


def render_list(tasks: list[dict[str, Any]], style: Style) -> str:
    if not tasks:
        return "No tasks yet.\n"
    return "\n".join(one_line(t, style) for t in tasks) + "\n"


def render_full(task: dict[str, Any], style: Style) -> str:
    bar = style.dim("-" * 78)
    head = (
        f"{style.bold(task['id'])}  {style.status(task['status'])} {route(task)}  "
        f"{duration(task)}  {style.dim(task.get('created') or '')}"
    )
    out = [bar, head, bar]
    prompt = task.get("prompt")
    if prompt:
        out += [style.bold("PROMPT"), prompt, ""]
    else:
        out += [
            style.bold("PROMPT") + style.dim(" (first 200 characters; set [server] store_prompts"),
            style.dim("= true to keep the full text sent to the model)"),
            task.get("description") or "",
            "",
        ]
    if task.get("error"):
        out += [style.bold("ERROR"), task["error"], ""]
    if task.get("result") is not None:
        out += [style.bold("RESULT"), task["result"]]
    if task.get("saved_to"):
        out += ["", style.dim(f"Saved to: {task['saved_to']}")]
    return "\n".join(out) + "\n\n"


# -------------------------------------------------------------------- commands


def list_tasks(path: Path, limit: int, out: TextIO, style: Style) -> None:
    db = connect(path)
    out.write(render_list(fetch_recent(db, limit), style))


def show_task(path: Path, ident: str, out: TextIO, style: Style) -> None:
    out.write(render_full(find_task(connect(path), ident), style))


def watch(
    path: Path,
    out: TextIO,
    style: Style,
    interval: float = 1.0,
    limit: int = 10,
    max_ticks: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Follow the queue live: announce new tasks and print each one in full when it ends."""
    db = connect(path)
    first = fetch_recent(db, RECENT_WINDOW)
    seen = {t["id"]: t["status"] for t in first}
    out.write(style.dim(f"Watching {path}  (Ctrl+C to stop)") + "\n")
    out.write(render_list(list(reversed(first[:limit])), style))
    out.flush()
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        sleep(interval)
        ticks += 1
        for task in reversed(fetch_recent(db, RECENT_WINDOW)):  # oldest first
            previous = seen.get(task["id"])
            if previous == task["status"]:
                continue
            seen[task["id"]] = task["status"]
            if task["status"] in TERMINAL_STATES:
                out.write(render_full(task, style))
            elif previous is None:
                out.write(one_line(task, style) + "\n")
        out.flush()


def configure_output() -> None:
    """Never crash on characters the console cannot encode."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
