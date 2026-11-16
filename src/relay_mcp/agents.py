"""Agent profiles and skills: Markdown files read from disk.

An agent pairs a system prompt with a model or tier:

    ---
    name: explorer
    description: Maps a codebase and reports what is where. Use before changing anything.
    model: claude/haiku
    include: src/**/*.py, README.md
    skills: security
    ---
    You are a code explorer. Read what you are given and report ...

The front matter is a flat list of `key: value` lines (no YAML needed). The body is the
system prompt. Files are read on every call, so edits apply without restarting the server.

A skill is a folder with a `SKILL.md` (the same layout Antigravity and Claude Code use):
reusable instructions that agents opt into with `skills:`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
KNOWN_KEYS = {"name", "description", "model", "tier", "include", "skills"}


class AgentError(ValueError):
    """An agent or skill file is invalid."""


@dataclass(frozen=True)
class Agent:
    name: str
    description: str
    system: str
    destinations: tuple[str, ...] | None  # explicit "backend/model" list, or None to use `tier`
    tier: str | None
    source: Path | None = None
    include: tuple[str, ...] = ()  # glob patterns, relative to the project root
    skills: tuple[str, ...] = ()
    scope: str = "global"  # "global" (next to relay.toml) or "project" (<repo>/.agents/relay)

    @property
    def route(self) -> str:
        return ", ".join(self.destinations) if self.destinations else f"tier:{self.tier}"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    source: Path | None = None
    scope: str = "global"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _items(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def split_front_matter(
    text: str, known: set[str] | None = KNOWN_KEYS
) -> tuple[dict[str, str], str]:
    """Split `---` front matter from the body.

    With `known` set (agents) unknown keys and malformed lines are errors, which catches typos.
    With `known=None` (skills) anything unrecognised is ignored, so skills written for other
    tools load as they are.
    """
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
            if known is None:
                continue
            raise AgentError(f"front matter line '{line}' must look like 'key: value'")
        key = key.strip().lower()
        if known is not None and key not in known:
            raise AgentError(f"unknown key '{key}' (allowed: {', '.join(sorted(known))})")
        meta[key] = _unquote(value)
    return meta, "\n".join(lines[end + 1 :]).strip()


def validate_glob(pattern: str) -> None:
    """Include patterns must stay inside the project: relative, with no `..`."""
    normalised = pattern.replace("\\", "/")
    parts = PurePosixPath(normalised).parts
    if normalised.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", normalised) or ".." in parts:
        raise AgentError(
            f"include pattern '{pattern}' must be relative and stay inside the project"
        )


def parse_agent(
    text: str,
    default_name: str,
    tiers: list[str],
    backends: set[str],
    source: Path | None = None,
    scope: str = "global",
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
        destinations = _items(meta["model"])
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

    include = _items(meta.get("include", ""))
    for pattern in include:
        validate_glob(pattern)
    skills = tuple(s.lower() for s in _items(meta.get("skills", "")))
    for skill in skills:
        if not SKILL_NAME_RE.match(skill):
            raise AgentError(f"skill name '{skill}' must be lowercase letters, digits, '-' or '_'")
    return Agent(
        name, meta.get("description", ""), body, destinations, tier, source, include, skills, scope
    )


def load_agents(
    directory: Path, tiers: list[str], backends: set[str], scope: str = "global"
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
                path.read_text(encoding="utf-8-sig"),
                path.stem.lower(),
                tiers,
                backends,
                path,
                scope,
            )
            if agent.name in agents:
                raise AgentError(
                    f"name '{agent.name}' is already used by {agents[agent.name].source}"
                )
            agents[agent.name] = agent
        except (AgentError, OSError, UnicodeDecodeError) as e:
            errors[path.name] = str(e)
    return agents, errors


def parse_skill(text: str, default_name: str, source: Path | None, scope: str) -> Skill:
    text = text.lstrip("﻿")
    if text.lstrip().startswith("---"):
        meta, body = split_front_matter(text, known=None)
    else:  # a bare SKILL.md without front matter is accepted
        meta, body = {}, text.strip()
    name = meta.get("name", default_name).lower()
    if not SKILL_NAME_RE.match(name):
        raise AgentError(f"name '{name}' must be lowercase letters, digits, '-' or '_'")
    if not body:
        raise AgentError("the skill has no instructions")
    return Skill(name, meta.get("description", ""), body, source, scope)


def load_skills(
    directories: list[Path], scope: str = "global"
) -> tuple[dict[str, Skill], dict[str, str]]:
    """Read `<dir>/<name>/SKILL.md` from each directory. Earlier directories win on a name clash.

    The same skill reachable from two folders (for example `.agents/skills` and
    `.claude/skills`) is not an error: the first copy is used.
    """
    skills: dict[str, Skill] = {}
    errors: dict[str, str] = {}
    for directory in directories:
        if not directory.is_dir():
            continue
        for folder in sorted(p for p in directory.iterdir() if p.is_dir()):
            file = folder / "SKILL.md"
            if not file.is_file():
                continue
            try:
                skill = parse_skill(
                    file.read_text(encoding="utf-8-sig"), folder.name.lower(), file, scope
                )
            except (AgentError, OSError, UnicodeDecodeError) as e:
                errors[f"{folder.name}/SKILL.md"] = str(e)
                continue
            skills.setdefault(skill.name, skill)
    return skills, errors
