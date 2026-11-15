"""The MCP server: tools exposed to clients."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.fastmcp import FastMCP

from .backends import Backend, Router, build_backends
from .config import Config
from .security import PathNotAllowed, PathPolicy
from .store import TaskStore
from .tasks import TaskManager

INSTRUCTIONS = (
    "Delegate work to cheaper or local models. Pass absolute file paths instead of pasting file "
    "contents, and use save_to for long results (a new file; existing files are never "
    "overwritten). "
    "If a task comes back with status 'running', collect it later with task_status. "
    "Call list_tiers to see which tiers exist."
)


class Relay:
    """Application logic, independent of the MCP transport (and therefore easy to test)."""

    def __init__(
        self,
        config: Config,
        backends: dict[str, Backend] | None = None,
        store: TaskStore | None = None,
    ):
        self.config = config
        self.policy = PathPolicy(config.security.allowed_roots, config.security.deny_patterns)
        self.backends = backends if backends is not None else build_backends(config)
        self.router = Router(self.backends, config.tiers)
        self.store = store or TaskStore(config.server.db)
        self.store.interrupt_stale()
        self.store.prune(config.server.retention_days)
        self.tasks = TaskManager(
            self.store, config.server.max_concurrent, config.server.max_wait_seconds
        )

    def read_attachments(self, paths: list[str]) -> str:
        parts, total = [], 0
        limit = self.config.server.max_attachment_chars
        for raw in paths:
            path = self.policy.check(raw)
            if not path.is_file():
                raise ValueError(f"Not a file: {path}")
            data = path.read_bytes()
            if b"\x00" in data[:4096]:
                raise ValueError(f"{path} looks binary; only text files can be attached")
            text = data.decode("utf-8", errors="replace")
            total += len(text)
            if total > limit:
                raise ValueError(f"Attachments exceed the limit of {limit} characters")
            parts.append(f'<file path="{path}">\n{text}\n</file>')
        return "\n\n".join(parts)

    async def delegate(
        self,
        prompt: str,
        files: list[str] | None = None,
        tier: str = "light",
        system: str | None = None,
        save_to: str | None = None,
        wait_seconds: int = 45,
    ) -> dict[str, Any]:
        if tier not in self.config.tiers:
            raise ValueError(f"Unknown tier '{tier}'. Available: {', '.join(self.config.tiers)}")
        attachments = self.read_attachments(files or [])
        target = self.policy.check_output(save_to) if save_to else None
        messages = [{"role": "system", "content": system}] if system else []
        messages.append(
            {"role": "user", "content": f"{attachments}\n\n{prompt}" if attachments else prompt}
        )
        return await self.tasks.submit(
            "text", tier, prompt, lambda: self.router.chat(tier, messages), target, wait_seconds
        )

    async def transcribe(
        self,
        audio_path: str,
        language: str | None = None,
        save_to: str | None = None,
        wait_seconds: int = 45,
    ) -> dict[str, Any]:
        cfg = self.config.transcription
        if not cfg:
            raise ValueError("Transcription is not configured: add a [transcription] section")
        path = self.policy.check(audio_path)
        if not path.is_file():
            raise ValueError(f"Not a file: {path}")
        target = self.policy.check_output(save_to) if save_to else None
        backend, model = self.backends[cfg["backend"]], cfg["model"]

        async def work() -> tuple[str, str]:
            return await backend.transcribe(path, model, language), f"{cfg['backend']}/{model}"

        return await self.tasks.submit("transcription", "-", path.name, work, target, wait_seconds)

    async def list_models(self) -> dict[str, Any]:
        names = list(self.backends)
        results = await asyncio.gather(*(self.backends[n].list_models() for n in names))
        return {
            "backends": dict(zip(names, results, strict=True)),
            "tiers": self.config.tiers,
            "transcription": self.config.transcription,
        }


def create_server(relay: Relay, host: str | None = None, port: int | None = None) -> FastMCP:
    srv = relay.config.server
    mcp = FastMCP(
        "relay",
        instructions=INSTRUCTIONS,
        host=host or srv.host,
        port=port or srv.port,
    )

    @mcp.tool()
    async def delegate(
        prompt: str,
        files: list[str] | None = None,
        tier: str = "light",
        system: str | None = None,
        save_to: str | None = None,
        wait_seconds: int = 45,
    ) -> dict:
        """Send a text task to another model (summarise, classify, extract, draft...).

        prompt: complete instructions; the other model does not see this conversation.
        files: absolute paths of text files the server attaches to the prompt.
        tier: which group of models to use (see list_tiers). Default 'light'.
        system: optional system prompt.
        save_to: absolute path of a NEW file to write the result to; the reply then holds only
            an excerpt.
        wait_seconds: how long to wait before returning an id to poll (0 = do not wait).
        """
        return await relay.delegate(prompt, files, tier, system, save_to, wait_seconds)

    @mcp.tool()
    async def transcribe(
        audio_path: str,
        language: str | None = None,
        save_to: str | None = None,
        wait_seconds: int = 45,
    ) -> dict:
        """Transcribe an audio file (absolute path). language: ISO code such as 'en'."""
        return await relay.transcribe(audio_path, language, save_to, wait_seconds)

    @mcp.tool()
    async def task_status(id: str, wait_seconds: int = 0) -> dict:
        """Status and result of a task. With wait_seconds, waits for it to finish (max 300)."""
        return await relay.tasks.wait_for(id, wait_seconds)

    @mcp.tool()
    async def list_tasks(limit: int = 20) -> list[dict]:
        """Most recent tasks, newest first (results truncated)."""
        return [relay.tasks.view(i, full=False) for i in relay.store.recent(limit)]

    @mcp.tool()
    async def cancel_task(id: str) -> dict:
        """Cancel a pending or running task."""
        return await relay.tasks.cancel(id)

    @mcp.tool()
    async def list_tiers() -> dict:
        """Available tiers and the models behind each, in order of preference."""
        return {"tiers": relay.config.tiers}

    @mcp.tool()
    async def list_models() -> dict:
        """Live list of the models every configured backend offers, plus the tier mapping."""
        return await relay.list_models()

    return mcp


__all__ = ["Relay", "create_server", "PathNotAllowed"]
