"""Background task execution on top of the store."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .backends import AllFailed, Completion
from .store import TaskStore, now

# Returns a Completion (text, destination and usage), or just (text, "backend/model") when
# there is no token usage to record (audio transcription).
Work = Callable[[], Awaitable[Completion | tuple[str, str]]]
EXCERPT_CHARS = 400


def usage_fields(completion: Completion) -> dict[str, Any]:
    """The database columns describing what a finished task consumed."""
    everything = completion.usage + completion.wasted
    return {
        "input_tokens": completion.usage.input_tokens,
        "output_tokens": completion.usage.output_tokens,
        "cache_tokens": completion.usage.cache_tokens,
        "wasted_tokens": completion.wasted.total,
        "cost_usd": everything.cost_usd,
        "usage_estimated": int(everything.estimated),
        "attempts": completion.attempts,
    }


class TaskManager:
    def __init__(self, store: TaskStore, max_concurrent: int = 3, max_wait: int = 300):
        self.store = store
        self.max_wait = max_wait
        self._slots = asyncio.Semaphore(max_concurrent)
        self._running: dict[str, asyncio.Task[None]] = {}

    def view(self, task_id: str, full: bool = True) -> dict[str, Any]:
        task = self.store.get(task_id)
        if task is None:
            raise ValueError(f"No such task: {task_id}")
        task.pop("prompt", None)  # stays local: only the viewer shows it, never the MCP client
        result = task.get("result")
        if result and (not full or task.get("saved_to")) and len(result) > EXCERPT_CHARS:
            task["result"] = result[:EXCERPT_CHARS] + "... (excerpt; full text in saved_to)"
        return task

    async def submit(
        self,
        kind: str,
        tier: str,
        description: str,
        work: Work,
        save_to: Path | None,
        wait: int,
        prompt: str | None = None,
        agent: str | None = None,
        thread: str | None = None,
    ) -> dict[str, Any]:
        task_id = uuid.uuid4().hex[:8]
        self.store.insert(task_id, kind, tier, description, prompt, agent, thread)

        async def run() -> None:
            try:
                async with self._slots:
                    self.store.update(task_id, status="running")
                    outcome = await work()
                if isinstance(outcome, Completion):
                    text, model, usage = outcome.text, outcome.destination, usage_fields(outcome)
                else:
                    (text, model), usage = outcome, {}
                if save_to:
                    save_to.parent.mkdir(parents=True, exist_ok=True)
                    with save_to.open("x", encoding="utf-8") as f:  # never overwrite
                        f.write(text)
                self.store.update(
                    task_id,
                    status="completed",
                    result=text,
                    model=model,
                    saved_to=str(save_to) if save_to else None,
                    finished=now(),
                    **usage,
                )
            except asyncio.CancelledError:
                self.store.update(task_id, status="cancelled", finished=now())
                raise
            except Exception as e:  # reported to the client as the task's state
                spent = {}
                if isinstance(e, AllFailed):  # the failed attempts may still have cost tokens
                    spent = {
                        "wasted_tokens": e.wasted.total,
                        "cost_usd": e.wasted.cost_usd,
                        "usage_estimated": int(e.wasted.estimated),
                        "attempts": e.attempts,
                    }
                self.store.update(
                    task_id,
                    status="failed",
                    error=f"{type(e).__name__}: {e}",
                    finished=now(),
                    **spent,
                )
            finally:
                self._running.pop(task_id, None)

        self._running[task_id] = asyncio.create_task(run())
        return await self.wait_for(task_id, wait)

    async def wait_for(self, task_id: str, seconds: int) -> dict[str, Any]:
        running = self._running.get(task_id)
        if running and seconds > 0:
            await asyncio.wait({running}, timeout=min(seconds, self.max_wait))
        task = self.view(task_id)
        if task["status"] in ("pending", "running"):
            task["note"] = "Still running: poll with task_status using this id."
        return task

    async def cancel(self, task_id: str) -> dict[str, Any]:
        running = self._running.get(task_id)
        if running:
            running.cancel()
            await asyncio.wait({running}, timeout=5)
            # A task cancelled before it ever started never runs its own handler.
            current = self.store.get(task_id)
            if current and current["status"] in ("pending", "running"):
                self.store.update(task_id, status="cancelled", finished=now())
        return self.view(task_id)
