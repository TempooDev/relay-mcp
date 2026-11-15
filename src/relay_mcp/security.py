"""Filesystem policy: what the server may read and write on behalf of a client."""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable
from pathlib import Path

# Files the server creates may only have one of these extensions. An allow-list is used on
# purpose: a deny-list of "dangerous" extensions is always incomplete (.bat, .ps1, .hta, .reg...).
DEFAULT_OUTPUT_EXTENSIONS = (
    ".md", ".txt", ".json", ".csv", ".tsv", ".html", ".htm", ".xml",
    ".yaml", ".yml", ".log", ".srt", ".vtt", ".rst", ".tex",
)  # fmt: skip


class PathNotAllowed(PermissionError):
    """The path is outside the allowed roots or matches a deny pattern."""


class PathPolicy:
    def __init__(
        self,
        roots: Iterable[Path],
        deny_patterns: Iterable[str],
        output_extensions: Iterable[str] = DEFAULT_OUTPUT_EXTENSIONS,
    ):
        self.roots = [Path(r).expanduser().resolve() for r in roots]
        self.deny = [p.lower() for p in deny_patterns]
        self.output_extensions = {e.lower() for e in output_extensions}

    def check(self, raw: str) -> Path:
        """Resolve `raw` (symlinks included) and verify it is allowed. File need not exist."""
        p = Path(raw).expanduser()
        if not p.is_absolute():
            raise PathNotAllowed(f"Use an absolute path (got '{raw}')")
        real = p.resolve()
        for root in self.roots:
            if real == root or real.is_relative_to(root):
                for part in real.relative_to(root).parts:
                    # On Windows ':' selects an NTFS alternate data stream (file.txt:stream).
                    if os.name == "nt" and ":" in part:
                        raise PathNotAllowed(f"'{part}': ':' is not allowed in file names")
                    if any(fnmatch.fnmatchcase(part.lower(), pat) for pat in self.deny):
                        raise PathNotAllowed(f"'{real}' matches a protected name ('{part}')")
                return real
        allowed = ", ".join(str(r) for r in self.roots)
        raise PathNotAllowed(f"'{real}' is outside the allowed folders ({allowed})")

    def check_output(self, raw: str) -> Path:
        """Like `check`, for files the server will create. Never overwrites."""
        real = self.check(raw)
        # Windows silently drops trailing dots and spaces ("run.bat." creates run.bat).
        if real.name != real.name.rstrip(". "):
            raise PathNotAllowed("File names cannot end with a dot or a space")
        if real.suffix.lower() not in self.output_extensions:
            allowed = ", ".join(sorted(self.output_extensions))
            raise PathNotAllowed(
                f"Cannot write '{real.name}': extension must be one of {allowed} "
                "(configurable with [security] output_extensions)"
            )
        if real.exists():
            raise PathNotAllowed(f"'{real}' already exists; choose a new file name")
        return real
