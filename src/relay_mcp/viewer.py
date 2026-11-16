"""Read-only viewer for the task database: `relay-mcp tasks | show | watch | usage`.

It reads the SQLite file directly, so it works from any terminal, in parallel with the
server, and even when the server is stopped. It never writes.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
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


def compact(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def tokens_text(task: dict[str, Any]) -> str:
    """`in>out` in a few characters, `~` when the numbers are estimates, `-` when unknown."""
    if task.get("input_tokens") is None:
        return "-"
    mark = "~" if task.get("usage_estimated") else ""
    return f"{mark}{compact(task['input_tokens'])}>{compact(task.get('output_tokens') or 0)}"


def usage_line(task: dict[str, Any]) -> str:
    """The consumption of one task, in full, for `show`; empty when nothing was recorded."""
    if task.get("input_tokens") is None and not task.get("wasted_tokens"):
        return ""
    parts = []
    if task.get("input_tokens") is not None:
        cached = task.get("cache_tokens") or 0
        cache = f" ({cached:,} cached)" if cached else ""
        parts.append(f"in {task['input_tokens']:,}{cache}  out {task.get('output_tokens') or 0:,}")
    if task.get("wasted_tokens"):
        parts.append(f"wasted {task['wasted_tokens']:,} on failed attempts")
    if task.get("cost_usd") is not None:
        parts.append(f"${task['cost_usd']:.4f}")
    if task.get("attempts"):
        parts.append(f"{task['attempts']} attempt{'s' if task['attempts'] != 1 else ''}")
    if task.get("usage_estimated"):
        parts.append("estimated, not reported by the backend")
    return "Usage: " + "  |  ".join(parts)


def one_line(task: dict[str, Any], style: Style, width: int = 60) -> str:
    desc = " ".join((task.get("description") or "").split())
    if len(desc) > width:
        desc = desc[: width - 3] + "..."
    return (
        f"{style.dim(clock(task))}  {style.bold(task['id'])}  {style.status(task['status'])} "
        f"{route(task):<42} {duration(task):>7} {tokens_text(task):>11}  {desc}"
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
    out = [bar, head]
    if usage := usage_line(task):
        out.append(style.dim(usage))
    out.append(bar)
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


# ----------------------------------------------------------------------- usage

USAGE_KEYS = {
    "tier": "tier",
    "model": "COALESCE(model, '-')",
    "agent": "COALESCE(agent, '-')",
    "day": "substr(created, 1, 10)",
}


def parse_since(text: str, now: datetime | None = None) -> datetime | None:
    """`24h`, `7d`, `2w` -> the moment that long ago; `all` -> None (no limit)."""
    value = text.strip().lower()
    if value == "all":
        return None
    match = re.fullmatch(r"(\d+)([hdw])", value)
    if not match:
        raise ValueError(f"'{text}' is not a period: use something like 24h, 7d, 2w or all")
    unit = {"h": "hours", "d": "days", "w": "weeks"}[match.group(2)]
    return (now or datetime.now()) - timedelta(**{unit: int(match.group(1))})


def usage_rows(db: sqlite3.Connection, since: datetime | None, by: str) -> list[dict[str, Any]]:
    """Consumption of text tasks grouped by `tier`, `model`, `agent` or `day`."""
    if by not in USAGE_KEYS:
        raise ValueError(f"by must be one of: {', '.join(USAGE_KEYS)}")
    where = "kind = 'text' AND (input_tokens IS NOT NULL OR wasted_tokens IS NOT NULL)"
    args: list[Any] = []
    if since is not None:
        where += " AND created >= ?"
        args.append(since.isoformat(timespec="seconds"))
    rows = db.execute(
        f"""SELECT {USAGE_KEYS[by]} AS key, COUNT(*) AS tasks,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(cache_tokens), 0) AS cache_tokens,
                   COALESCE(SUM(wasted_tokens), 0) AS wasted_tokens,
                   SUM(cost_usd) AS cost_usd,
                   COALESCE(SUM(usage_estimated), 0) AS estimated_tasks
            FROM tasks WHERE {where} GROUP BY key ORDER BY key""",
        args,
    ).fetchall()
    return [dict(r) for r in rows]


def usage_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    return {
        "tasks": sum(r["tasks"] for r in rows),
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
        "cache_tokens": sum(r["cache_tokens"] for r in rows),
        "wasted_tokens": sum(r["wasted_tokens"] for r in rows),
        "cost_usd": round(sum(costs), 6) if costs else None,
        "estimated_tasks": sum(r["estimated_tasks"] for r in rows),
    }


def tasks_without_usage(db: sqlite3.Connection, since: datetime | None) -> int:
    """Finished text tasks with no usage data: they ran before usage tracking existed."""
    where = (
        "kind = 'text' AND status IN ('completed', 'failed') "
        "AND input_tokens IS NULL AND wasted_tokens IS NULL"
    )
    args: list[Any] = []
    if since is not None:
        where += " AND created >= ?"
        args.append(since.isoformat(timespec="seconds"))
    return int(db.execute(f"SELECT COUNT(*) FROM tasks WHERE {where}", args).fetchone()[0])


def render_usage(
    rows: list[dict[str, Any]], by: str, period: str, style: Style, missing: int = 0
) -> str:
    scope = "all time" if period == "all" else f"the last {period}"
    if not rows:
        if missing:
            return (
                f"No usage recorded for {scope}.\n"
                f"{missing} finished task(s) in that period have no usage data: they ran before "
                "usage tracking was added. New tasks are recorded automatically.\n"
            )
        return f"No tasks in {scope} yet.\n"
    width = min(44, max([len(r["key"]) for r in rows] + [len(by), 5]) + 2)  # name column
    header = (
        f"{by:<{width}}{'tasks':>6}{'input':>12}{'output':>10}{'wasted':>10}{'cost':>11}{'est':>5}"
    )
    title = "all time" if period == "all" else f"last {period}"
    lines = [style.bold(f"Usage by {by} ({title})"), style.dim(header)]

    def row(label: str, r: dict[str, Any]) -> str:
        cost = f"${r['cost_usd']:.4f}" if r["cost_usd"] is not None else "-"
        return (
            f"{label[: width - 1]:<{width}}{r['tasks']:>6}{r['input_tokens']:>12,}"
            f"{r['output_tokens']:>10,}"
            f"{r['wasted_tokens']:>10,}{cost:>11}{r['estimated_tasks']:>5}"
        )

    lines += [row(r["key"], r) for r in rows]
    lines += [style.dim("-" * len(header)), style.bold(row("TOTAL", usage_totals(rows)))]
    if missing:
        lines += [
            "",
            style.dim(f"{missing} earlier task(s) have no usage data and are not counted"),
        ]
    lines += [
        "",
        style.dim("input includes cached tokens; wasted = tokens spent by attempts that failed"),
        style.dim(
            "cost: only for backends that report it (claude), at list price; - = not reported"
        ),
        style.dim("est: tasks whose tokens were estimated (about 4 characters per token)"),
    ]
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------- commands


def show_usage(path: Path, since: str, by: str, out: TextIO, style: Style) -> None:
    db = connect(path)
    try:
        cutoff = parse_since(since)
    except ValueError as e:
        raise ViewerError(str(e)) from e
    try:
        rows = usage_rows(db, cutoff, by)
        missing = tasks_without_usage(db, cutoff)
    except sqlite3.OperationalError as e:
        if "no such column" not in str(e):
            raise
        raise ViewerError(
            "This task database was created by an older relay-mcp and has no usage columns yet. "
            "Start the server once with the current version (it upgrades the database on "
            "startup), then try again."
        ) from e
    out.write(render_usage(rows, by, since, style, missing))


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
