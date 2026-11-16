"""Model backends (OpenAI-compatible HTTP endpoints and command-line tools) and tier routing."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .config import Config

Messages = list[dict[str, str]]


@dataclass
class Usage:
    """Tokens (and cost, when the backend reports it) spent by one or more model calls."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0  # the part of input_tokens served from or written to a cache
    cost_usd: float | None = None  # None: the backend did not report a cost
    estimated: bool = False  # True when the numbers are a guess (about 4 characters per token)

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        cost = None
        if self.cost_usd is not None or other.cost_usd is not None:
            cost = (self.cost_usd or 0.0) + (other.cost_usd or 0.0)
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_tokens + other.cache_tokens,
            cost,
            self.estimated or other.estimated,
        )


@dataclass
class Reply:
    """What a backend returns. `usage` is None when the backend does not say."""

    text: str
    usage: Usage | None = None


@dataclass
class Completion:
    """The outcome of a routed call: the answer, who gave it, and what it cost."""

    text: str
    destination: str  # "backend/model"
    usage: Usage  # the attempt that answered
    wasted: Usage = field(default_factory=Usage)  # attempts that failed first
    attempts: int = 1


class BackendError(RuntimeError):
    """The backend was reachable in principle but the request failed."""

    def __init__(self, message: str, usage: Usage | None = None):
        super().__init__(message)
        self.usage = usage  # tokens a failed call still consumed, when known


class Unavailable(BackendError):
    """The backend cannot be used right now (no key, CLI not installed...). Skipped silently."""


class AllFailed(BackendError):
    """Every destination failed. Carries what the failed attempts still consumed."""

    def __init__(self, message: str, wasted: Usage, attempts: int):
        super().__init__(message)
        self.wasted = wasted
        self.attempts = attempts


def estimate_tokens(text: str) -> int:
    """A rough count (about 4 characters per token) for backends that report no usage."""
    return (len(text) + 3) // 4


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


def _number(value: Any) -> int:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0


def usage_from_openai(usage: Any) -> Usage | None:
    """The `usage` object of an OpenAI-style response."""
    if not isinstance(usage, dict):
        return None
    details = usage.get("prompt_tokens_details")
    cached = _number(details.get("cached_tokens")) if isinstance(details, dict) else 0
    return Usage(
        _number(usage.get("prompt_tokens")), _number(usage.get("completion_tokens")), cached
    )


def usage_from_cli_json(usage: Any, cost: Any = None) -> Usage | None:
    """The `usage` object printed by `claude -p` / `agy -p` with `--output-format json`."""
    if not isinstance(usage, dict):
        return None
    cost_usd = float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else None
    if "cache_creation_input_tokens" in usage or "cache_read_input_tokens" in usage:
        # Anthropic reports cached tokens separately from input_tokens: add them back, since
        # they are all part of the prompt the model processed.
        cache = _number(usage.get("cache_creation_input_tokens")) + _number(
            usage.get("cache_read_input_tokens")
        )
        return Usage(
            _number(usage.get("input_tokens")) + cache,
            _number(usage.get("output_tokens")),
            cache,
            cost_usd,
        )
    return Usage(
        _number(usage.get("input_tokens")),
        _number(usage.get("output_tokens")),
        _number(usage.get("cache_read_tokens")),
        cost_usd,
    )


class Backend(ABC):
    def __init__(self, name: str, cfg: dict[str, Any]):
        self.name = name
        self.cfg = cfg

    @abstractmethod
    async def chat(self, model: str, messages: Messages) -> str | Reply: ...

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

    async def chat(self, model: str, messages: Messages) -> Reply:
        headers = self._headers()
        try:
            r = await self.client.post(
                f"{self.base}/chat/completions",
                headers=headers,
                json={"model": model, "messages": messages},
            )
            r.raise_for_status()
            data = r.json()
            text = data["choices"][0]["message"]["content"] or ""
            return Reply(text, usage_from_openai(data.get("usage")))
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

    @staticmethod
    def _parse_json(raw: str) -> Reply:
        """Read the answer and its usage from `--output-format json` output.

        Both `claude -p` (`result`, `is_error`, `total_cost_usd`) and `agy -p` (`response`,
        `status`) are understood. Output that is not that JSON is treated as plain text, so a
        CLI that ignores the flag still works (with estimated usage).
        """
        try:
            data = json.loads(raw)
        except ValueError:
            return Reply(raw)
        if not isinstance(data, dict):
            return Reply(raw)
        text = data.get("result", data.get("response"))
        if not isinstance(text, str):
            return Reply(raw)
        text = text.strip()
        usage = usage_from_cli_json(data.get("usage"), data.get("total_cost_usd"))
        failed = data.get("is_error") is True or str(data.get("status", "SUCCESS")).upper() not in (
            "SUCCESS",
            "OK",
        )
        if failed:
            raise BackendError(f"the CLI reported an error: {text[:200]}", usage)
        return Reply(text, usage)

    async def chat(self, model: str, messages: Messages) -> Reply:
        exe = self._exe()
        prompt = flatten(messages)
        args = [str(a).replace("{model}", model) for a in self.cfg.get("args", [])]
        if self.cfg.get("output") == "json":
            args += [str(a) for a in self.cfg.get("json_args", ["--output-format", "json"])]
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
        raw = out.decode("utf-8", "replace").strip()
        reply = self._parse_json(raw) if self.cfg.get("output") == "json" and raw else Reply(raw)
        if not reply.text:
            raise BackendError(
                f"empty output: {err.decode('utf-8', 'replace')[:300].strip()}", reply.usage
            )
        # Some CLIs print provider errors (filters, quota...) and still exit with code 0.
        for pattern in self.cfg.get("error_patterns", []):
            if re.search(pattern, reply.text, re.IGNORECASE):
                raise BackendError(
                    f"the CLI reported an error instead of an answer: {reply.text[:200]}",
                    reply.usage,
                )
        return reply

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

    async def chat(self, tier: str, messages: Messages) -> Completion:
        """Like `chat_to`, with the destinations of a configured tier."""
        return await self.chat_to(self.tiers[tier], messages)

    async def chat_to(self, destinations: Sequence[str], messages: Messages) -> Completion:
        """Answer from the first destination that works, counting what failed attempts cost."""
        failures = []
        wasted, attempts = Usage(), 0
        for dest in destinations:
            name, _, model = dest.partition("/")
            try:
                reply = await self.backends[name].chat(model, messages)
            except Unavailable as e:  # never tried: no tokens spent, not an attempt
                failures.append(f"{dest}: skipped ({e})")
                continue
            except BackendError as e:
                attempts += 1
                if e.usage is not None:
                    wasted = wasted + e.usage
                failures.append(f"{dest}: {e}")
                continue
            attempts += 1
            if isinstance(reply, str):  # simple backends (and test doubles) return plain text
                reply = Reply(reply)
            usage = reply.usage or Usage(
                estimate_tokens(flatten(messages)), estimate_tokens(reply.text), estimated=True
            )
            return Completion(reply.text, dest, usage, wasted, attempts)
        raise AllFailed("No destination answered. " + " | ".join(failures), wasted, attempts)
