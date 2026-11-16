"""Agent profiles: Markdown files that pair a system prompt with a model or tier.

    ---
    name: explorer
    description: Maps a codebase and reports what is where. Use before changing anything.
    model: claude/haiku
    ---
    You are a code explorer. Read what you are given and report ...

The front matter is a flat list of `key: value` lines (no YAML needed). The body is the
system prompt. Files are read on every call, so edits apply without restarting the server.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
KNOWN_KEYS = {"name", "description", "model", "tier"}


class AgentError(ValueError):
    """An agent file is invalid."""


@dataclass(frozen=True)
class Agent:
    name: str
    description: str
    system: str
    destinations: tuple[str, ...] | None  # explicit "backend/model" list, or None to use `tier`
    tier: str | None
    source: Path | None = None

    @property
    def route(self) -> str:
        return ", ".join(self.destinations) if self.destinations else f"tier:{self.tier}"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def split_front_matter(text: str) -> tuple[dict[str, str], str]:
    text = text.lstrip("﻿")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise AgentError("must start with a '---' front matter block")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise AgentError("front matter is not closed with '---'") from None
    meta: dict[str, str] = {}
    for raw in lines[1:end]:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise AgentError(f"front matter line '{line}' must look like 'key: value'")
        key = key.strip().lower()
        if key not in KNOWN_KEYS:
            raise AgentError(f"unknown key '{key}' (allowed: {', '.join(sorted(KNOWN_KEYS))})")
        meta[key] = _unquote(value)
    return meta, "\n".join(lines[end + 1 :]).strip()


def parse_agent(
    text: str,
    default_name: str,
    tiers: list[str],
    backends: set[str],
    source: Path | None = None,
) -> Agent:
    meta, body = split_front_matter(text)
    name = meta.get("name", default_name).lower()
    if not NAME_RE.match(name):
        raise AgentError(f"name '{name}' must be lowercase letters, digits, '-' or '_'")
    if not body:
        raise AgentError("the body (the system prompt) is empty")
    if "model" in meta and "tier" in meta:
        raise AgentError("use either 'model' or 'tier', not both")

    destinations: tuple[str, ...] | None = None
    tier: str | None = None
    if "model" in meta:
        destinations = tuple(d.strip() for d in meta["model"].split(",") if d.strip())
        if not destinations:
            raise AgentError("'model' is empty")
        for dest in destinations:
            backend, sep, model = dest.partition("/")
            if not sep or not model:
                raise AgentError(f"model '{dest}' must look like 'backend/model'")
            if backend not in backends:
                raise AgentError(f"model '{dest}': unknown backend '{backend}'")
    else:
        tier = meta.get("tier") or ("light" if "light" in tiers else tiers[0])
        if tier not in tiers:
            raise AgentError(f"unknown tier '{tier}' (available: {', '.join(tiers)})")
    return Agent(name, meta.get("description", ""), body, destinations, tier, source)


def load_agents(
    directory: Path, tiers: list[str], backends: set[str]
) -> tuple[dict[str, Agent], dict[str, str]]:
    """Read every *.md file in `directory`. Returns (agents by name, errors by file name).

    A broken file never hides the good ones: it is reported in the errors instead.
    """
    agents: dict[str, Agent] = {}
    errors: dict[str, str] = {}
    if not directory.is_dir():
        return agents, errors
    for path in sorted(directory.glob("*.md")):
        try:
            agent = parse_agent(
                path.read_text(encoding="utf-8-sig"), path.stem.lower(), tiers, backends, path
            )
            if agent.name in agents:
                raise AgentError(
                    f"name '{agent.name}' is already used by {agents[agent.name].source}"
                )
            agents[agent.name] = agent
        except (AgentError, OSError, UnicodeDecodeError) as e:
            errors[path.name] = str(e)
    return agents, errors
