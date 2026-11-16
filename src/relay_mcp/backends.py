"""Model backends (OpenAI-compatible HTTP endpoints and command-line tools) and tier routing."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from .config import Config

Messages = list[dict[str, str]]


class BackendError(RuntimeError):
    """The backend was reachable in principle but the request failed."""


class Unavailable(BackendError):
    """The backend cannot be used right now (no key, CLI not installed...). Skipped silently."""


def flatten(messages: Messages) -> str:
    """Collapse chat messages into a single prompt for command-line backends.

    A single-turn prompt is passed through untouched; once there is history, turns are labelled
    so the model can tell who said what.
    """
    if not any(m["role"] == "assistant" for m in messages):
        return "\n\n".join(m["content"] for m in messages)
    labels = {"user": "User", "assistant": "Assistant"}
    return "\n\n".join(
        m["content"] if m["role"] == "system" else f"{labels[m['role']]}: {m['content']}"
        for m in messages
    )


class Backend(ABC):
    def __init__(self, name: str, cfg: dict[str, Any]):
        self.name = name
        self.cfg = cfg

    @abstractmethod
    async def chat(self, model: str, messages: Messages) -> str: ...

    @abstractmethod
    async def list_models(self) -> dict[str, Any]: ...

    async def transcribe(self, path: Path, model: str, language: str | None) -> str:
        raise BackendError(f"Backend '{self.name}' does not support transcription")

    async def aclose(self) -> None:
        return None


class OpenAIBackend(Backend):
    """Any server that speaks the OpenAI REST API: Ollama, LiteLLM, vLLM, LM Studio, OpenAI..."""

    def __init__(self, name: str, cfg: dict[str, Any], client: httpx.AsyncClient | None = None):
        super().__init__(name, cfg)
        self.base = cfg["base_url"].rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(1800, connect=10))

    def _headers(self) -> dict[str, str]:
        env = self.cfg.get("api_key_env")
        key = os.environ.get(env) if env else self.cfg.get("api_key")
        if env and not key:
            raise Unavailable(f"environment variable {env} is not set")
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def chat(self, model: str, messages: Messages) -> str:
        headers = self._headers()
        try:
            r = await self.client.post(
                f"{self.base}/chat/completions",
                headers=headers,
                json={"model": model, "messages": messages},
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"] or ""
        except httpx.HTTPStatusError as e:
            raise BackendError(f"HTTP {e.response.status_code}: {e.response.text[:200]}") from e
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            raise BackendError(f"{type(e).__name__}: {e}") from e

    async def list_models(self) -> dict[str, Any]:
        try:
            r = await self.client.get(f"{self.base}/models", headers=self._headers(), timeout=10)
            r.raise_for_status()
            models = sorted(m["id"] for m in r.json()["data"])
            return {"status": "ok", "source": f"{self.base}/models", "models": models}
        except Unavailable as e:
            return {"status": f"skipped: {e}", "models": []}
        except Exception as e:  # one dead backend must not hide the others
            return {"status": f"error: {type(e).__name__}: {e}", "models": []}

    async def transcribe(self, path: Path, model: str, language: str | None) -> str:
        headers = self._headers()
        data = {"model": model, **({"language": language} if language else {})}
        try:
            r = await self.client.post(
                f"{self.base}/audio/transcriptions",
                headers=headers,
                data=data,
                files={"file": (path.name, path.read_bytes())},
            )
            r.raise_for_status()
            return r.json()["text"]
        except httpx.HTTPStatusError as e:
            raise BackendError(f"HTTP {e.response.status_code}: {e.response.text[:200]}") from e
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise BackendError(f"{type(e).__name__}: {e}") from e

    async def aclose(self) -> None:
        await self.client.aclose()


class CliBackend(Backend):
    """A non-interactive command-line tool, e.g. `claude -p` or `agy -p`."""

    def _exe(self) -> str:
        exe = shutil.which(self.cfg["command"])
        if not exe:
            raise Unavailable(f"'{self.cfg['command']}' was not found on PATH")
        return exe

    async def chat(self, model: str, messages: Messages) -> str:
        exe = self._exe()
        prompt = flatten(messages)
        args = [str(a).replace("{model}", model) for a in self.cfg.get("args", [])]
        stdin_data: bytes | None = None
        if flag := self.cfg.get("prompt_arg"):  # tools that cannot read the prompt from stdin
            limit = int(self.cfg.get("max_arg_chars", 28000))
            if len(prompt) > limit:
                raise BackendError(
                    f"prompt is {len(prompt)} characters; this CLI accepts at most {limit} "
                    "as an argument (use a different backend for large inputs)"
                )
            args.append(f"{flag}={prompt}")
        else:
            stdin_data = prompt.encode("utf-8")
        proc = await asyncio.create_subprocess_exec(
            exe,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin_data), timeout=float(self.cfg.get("timeout", 600))
            )
        except (TimeoutError, asyncio.CancelledError):
            proc.kill()
            raise
        if proc.returncode != 0:
            raise BackendError(
                f"exit code {proc.returncode}: {err.decode('utf-8', 'replace')[:300].strip()}"
            )
        text = out.decode("utf-8", "replace").strip()
        if not text:
            raise BackendError(f"empty output: {err.decode('utf-8', 'replace')[:300].strip()}")
        # Some CLIs print provider errors (filters, quota...) and still exit with code 0.
        for pattern in self.cfg.get("error_patterns", []):
            if re.search(pattern, text, re.IGNORECASE):
                raise BackendError(f"the CLI reported an error instead of an answer: {text[:200]}")
        return text

    async def list_models(self) -> dict[str, Any]:
        list_args = self.cfg.get("list_args")
        if not list_args:
            return {"status": "ok", "source": "config", "models": list(self.cfg.get("models", []))}
        try:
            proc = await asyncio.create_subprocess_exec(
                self._exe(),
                *list_args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except Unavailable as e:
            return {"status": f"skipped: {e}", "models": []}
        except Exception as e:
            return {"status": f"error: {type(e).__name__}: {e}", "models": []}
        rows = [ln.split("\t") for ln in out.decode("utf-8", "replace").splitlines() if ln.strip()]
        models = [f"{r[0]} ({r[1]})" if len(r) > 1 else r[0] for r in rows]
        return {
            "status": "ok",
            "source": " ".join([self.cfg["command"], *list_args]),
            "models": models,
        }


def build_backends(config: Config) -> dict[str, Backend]:
    kinds = {"openai": OpenAIBackend, "cli": CliBackend}
    return {name: kinds[cfg["type"]](name, cfg) for name, cfg in config.backends.items()}


class Router:
    """Resolves a tier to its ordered destinations and falls back on failure."""

    def __init__(self, backends: dict[str, Backend], tiers: dict[str, list[str]]):
        self.backends = backends
        self.tiers = tiers

    async def chat(self, tier: str, messages: Messages) -> tuple[str, str]:
        """Like `chat_to`, with the destinations of a configured tier."""
        return await self.chat_to(self.tiers[tier], messages)

    async def chat_to(self, destinations: Sequence[str], messages: Messages) -> tuple[str, str]:
        """Return (text, 'backend/model') from the first destination that answers."""
        failures = []
        for dest in destinations:
            name, _, model = dest.partition("/")
            try:
                return await self.backends[name].chat(model, messages), dest
            except Unavailable as e:
                failures.append(f"{dest}: skipped ({e})")
            except BackendError as e:
                failures.append(f"{dest}: {e}")
        raise BackendError("No destination answered. " + " | ".join(failures))
