"""Project scope: find a repository's root, decide whether to trust it, expand `include` globs.

A project keeps its relay material under `.agents/`:

    <repo>/.agents/relay/agents/*.md        agents (relay-specific)
    <repo>/.agents/skills/<name>/SKILL.md   skills (shared with Antigravity)
    <repo>/.claude/skills/<name>/SKILL.md   skills (shared with Claude Code)
"""

from __future__ import annotations

from pathlib import Path

from .security import PathNotAllowed, PathPolicy

# Never descended into by `include` patterns.
IGNORED_DIRS = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
     ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox"}
)  # fmt: skip
MAX_INCLUDE_FILES = 200


def find_project_root(path: Path) -> Path:
    """Nearest folder at or above `path` that has `.git` or `.agents`; else `path` itself."""
    start = path if path.is_dir() else path.parent
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists() or (candidate / ".agents").is_dir():
            return candidate
    return start


def is_trusted(root: Path, trusted: list[Path]) -> bool:
    """Trusting a folder trusts everything below it."""
    resolved = [t.resolve() for t in trusted]
    return any(root == t or root.is_relative_to(t) for t in resolved)


def agents_dir(root: Path) -> Path:
    return root / ".agents" / "relay" / "agents"


def skill_dirs(root: Path) -> list[Path]:
    return [root / ".agents" / "skills", root / ".claude" / "skills"]


def expand_include(
    root: Path, patterns: tuple[str, ...], policy: PathPolicy
) -> list[tuple[Path, Path]]:
    """Files matched by the patterns, as (real path, path relative to the project root).

    Files the policy refuses (`.env`, keys, anything resolving outside the allowed folders)
    and files inside ignored folders are skipped silently, so a broad pattern such as `**/*`
    cannot pull secrets in.
    """
    found: dict[Path, Path] = {}
    for pattern in patterns:
        for candidate in root.glob(pattern.replace("\\", "/")):
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(root)
            if any(part in IGNORED_DIRS for part in relative.parts[:-1]):
                continue
            try:
                real = policy.check(str(candidate))
            except PathNotAllowed:
                continue
            found[real] = relative
            if len(found) > MAX_INCLUDE_FILES:
                raise ValueError(
                    f"include patterns match more than {MAX_INCLUDE_FILES} files; narrow them"
                )
    return sorted(found.items(), key=lambda item: str(item[1]).lower())
