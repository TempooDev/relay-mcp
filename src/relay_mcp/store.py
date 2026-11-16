"""SQLite persistence for the task queue."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

COLUMNS = (
    "id", "kind", "tier", "status", "description", "model",
    "result", "error", "saved_to", "created", "finished", "prompt",
)  # fmt: skip


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class TaskStore:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        try:  # prompts and results are stored in clear text: keep the file private (POSIX)
            path.chmod(0o600)
        except OSError:
            pass
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, kind TEXT, tier TEXT, status TEXT, description TEXT,
                model TEXT, result TEXT, error TEXT, saved_to TEXT, created TEXT, finished TEXT,
                prompt TEXT)"""
        )
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")}
        if "prompt" not in columns:  # database created by an earlier version
            self.db.execute("ALTER TABLE tasks ADD COLUMN prompt TEXT")

    def interrupt_stale(self) -> None:
        """Tasks left running by a previous process can never finish: mark them."""
        self.db.execute(
            "UPDATE tasks SET status='interrupted', "
            "error='Server restarted before the task finished', finished=? "
            "WHERE status IN ('pending', 'running')",
            (now(),),
        )

    def prune(self, days: int) -> None:
        if days > 0:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
            self.db.execute("DELETE FROM tasks WHERE created < ?", (cutoff,))

    def insert(
        self, task_id: str, kind: str, tier: str, description: str, prompt: str | None = None
    ) -> None:
        self.db.execute(
            "INSERT INTO tasks (id, kind, tier, status, description, created, prompt) "
            "VALUES (?,?,?,?,?,?,?)",
            (task_id, kind, tier, "pending", description[:200], now(), prompt),
        )

    def update(self, task_id: str, **fields: Any) -> None:
        unknown = set(fields) - set(COLUMNS)
        if unknown:
            raise ValueError(f"Unknown columns: {unknown}")
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), task_id))

    def get(self, task_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return {k: row[k] for k in row.keys() if row[k] is not None} if row else None

    def recent(self, limit: int) -> list[str]:
        rows = self.db.execute(
            "SELECT id FROM tasks ORDER BY created DESC, rowid DESC LIMIT ?", (limit,)
        )
        return [r["id"] for r in rows.fetchall()]
