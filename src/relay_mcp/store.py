"""SQLite persistence for the task queue and conversation threads."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Columns added after the first version: databases created earlier get them via ALTER TABLE.
ADDED_COLUMNS = {
    "prompt": "TEXT", "agent": "TEXT", "thread": "TEXT",
    "input_tokens": "INTEGER", "output_tokens": "INTEGER", "cache_tokens": "INTEGER",
    "wasted_tokens": "INTEGER", "cost_usd": "REAL", "usage_estimated": "INTEGER",
    "attempts": "INTEGER",
}  # fmt: skip
COLUMNS = (
    "id", "kind", "tier", "status", "description", "model",
    "result", "error", "saved_to", "created", "finished", *ADDED_COLUMNS,
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
                prompt TEXT, agent TEXT, thread TEXT, input_tokens INTEGER, output_tokens INTEGER,
                cache_tokens INTEGER, wasted_tokens INTEGER, cost_usd REAL,
                usage_estimated INTEGER, attempts INTEGER)"""
        )
        existing = {row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")}
        for column, kind in ADDED_COLUMNS.items():
            if column not in existing:  # database created by an earlier version
                self.db.execute(f"ALTER TABLE tasks ADD COLUMN {column} {kind}")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS thread_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT, thread TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL, created TEXT NOT NULL)"""
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_thread_turns ON thread_turns(thread)")

    # ------------------------------------------------------------------- tasks

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
            self.db.execute("DELETE FROM thread_turns WHERE created < ?", (cutoff,))

    def insert(
        self,
        task_id: str,
        kind: str,
        tier: str,
        description: str,
        prompt: str | None = None,
        agent: str | None = None,
        thread: str | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO tasks "
            "(id, kind, tier, status, description, created, prompt, agent, thread) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, kind, tier, "pending", description[:200], now(), prompt, agent, thread),
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

    def find(self, ident: str) -> dict[str, Any]:
        """Look a task up by full id or by a unique prefix of it."""
        exact = self.get(ident)
        if exact:
            return exact
        escaped = ident.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self.db.execute(
            "SELECT id FROM tasks WHERE id LIKE ? ESCAPE '\\' LIMIT 2", (escaped + "%",)
        ).fetchall()
        if not ident or not rows:
            raise ValueError(f"No such task: {ident}")
        if len(rows) > 1:
            raise ValueError(f"'{ident}' matches more than one task; use more characters")
        found = self.get(rows[0]["id"])
        assert found is not None
        return found

    def recent(self, limit: int) -> list[str]:
        rows = self.db.execute(
            "SELECT id FROM tasks ORDER BY created DESC, rowid DESC LIMIT ?", (limit,)
        )
        return [r["id"] for r in rows.fetchall()]

    # ----------------------------------------------------------------- threads

    def thread_messages(self, thread: str) -> list[dict[str, str]]:
        rows = self.db.execute(
            "SELECT role, content FROM thread_turns WHERE thread=? ORDER BY id", (thread,)
        )
        return [{"role": r["role"], "content": r["content"]} for r in rows.fetchall()]

    def append_turns(self, thread: str, user: str, assistant: str) -> None:
        stamp = now()
        self.db.execute("BEGIN")
        try:
            for role, content in (("user", user), ("assistant", assistant)):
                self.db.execute(
                    "INSERT INTO thread_turns (thread, role, content, created) VALUES (?,?,?,?)",
                    (thread, role, content, stamp),
                )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def clear_thread(self, thread: str) -> int:
        cur = self.db.execute("DELETE FROM thread_turns WHERE thread=?", (thread,))
        return cur.rowcount
