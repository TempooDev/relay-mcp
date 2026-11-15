"""Filesystem policy: what the server may read and write on behalf of a client."""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from pathlib import Path

# A model must never be able to plant something that runs by itself.
DANGEROUS_OUTPUT_SUFFIXES = frozenset(
    {".exe", ".dll", ".bat", ".cmd", ".ps1", ".vbs", ".lnk", ".scr", ".msi", ".jar"}
)


class PathNotAllowed(PermissionError):
    """The path is outside the allowed roots or matches a deny pattern."""


class PathPolicy:
    def __init__(self, roots: Iterable[Path], deny_patterns: Iterable[str]):
        self.roots = [Path(r).expanduser().resolve() for r in roots]
        self.deny = [p.lower() for p in deny_patterns]

    def check(self, raw: str) -> Path:
        """Resolve `raw` (symlinks included) and verify it is allowed. File need not exist."""
        p = Path(raw).expanduser()
        if not p.is_absolute():
            raise PathNotAllowed(f"Use an absolute path (got '{raw}')")
        real = p.resolve()
        for root in self.roots:
            if real == root or real.is_relative_to(root):
                for part in real.relative_to(root).parts:
                    if any(fnmatch.fnmatchcase(part.lower(), pat) for pat in self.deny):
                        raise PathNotAllowed(f"'{real}' matches a protected name ('{part}')")
                return real
        allowed = ", ".join(str(r) for r in self.roots)
        raise PathNotAllowed(f"'{real}' is outside the allowed folders ({allowed})")

    def check_output(self, raw: str) -> Path:
        """Like `check`, for files the server will create. Never overwrites."""
        real = self.check(raw)
        if real.suffix.lower() in DANGEROUS_OUTPUT_SUFFIXES:
            raise PathNotAllowed(f"Refusing to write an executable/script type ('{real.suffix}')")
        if real.exists():
            raise PathNotAllowed(f"'{real}' already exists; choose a new file name")
        return real
