"""Configuration loading and validation."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_VAR = "RELAY_MCP_CONFIG"
CONFIG_NAME = "relay.toml"

# Names that are never read or written, wherever they appear below an allowed root.
DEFAULT_DENY_PATTERNS = (
    ".ssh", ".aws", ".gnupg", ".kube", ".docker",
    ".env", ".env.*", "*.pem", "*.key", "*.kdbx", "id_rsa*", "id_ed25519*",
    ".netrc", ".git-credentials", ".npmrc", ".pypirc", "credentials*",
)  # fmt: skip

BACKEND_TYPES = ("openai", "cli")


class ConfigError(ValueError):
    """The configuration file is missing or invalid."""


def user_config_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "relay-mcp"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "relay-mcp"


def user_data_dir() -> Path:
    if os.name == "nt":
        return (
            Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "relay-mcp"
        )
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "relay-mcp"


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    db: Path = field(default_factory=lambda: user_data_dir() / "tasks.db")
    max_concurrent: int = 3
    max_attachment_chars: int = 400_000
    retention_days: int = 30
    max_wait_seconds: int = 300


@dataclass
class SecurityConfig:
    allowed_roots: list[Path] = field(default_factory=lambda: [Path.home()])
    deny_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_DENY_PATTERNS))


@dataclass
class Config:
    server: ServerConfig
    security: SecurityConfig
    backends: dict[str, dict[str, Any]]
    tiers: dict[str, list[str]]
    transcription: dict[str, Any] | None = None
    source: Path | None = None


def candidate_paths(explicit: str | os.PathLike[str] | None = None) -> list[Path]:
    """Locations searched for the config file, in priority order."""
    if explicit:
        return [Path(explicit)]
    paths = []
    if env := os.environ.get(ENV_VAR):
        paths.append(Path(env))
    paths += [Path.cwd() / CONFIG_NAME, user_config_dir() / "config.toml"]
    return paths


def find_config(explicit: str | os.PathLike[str] | None = None) -> Path:
    candidates = candidate_paths(explicit)
    for p in candidates:
        if p.is_file():
            return p
    searched = "\n  ".join(str(p) for p in candidates)
    raise ConfigError(
        f"No config file found. Searched:\n  {searched}\n"
        "Create one with `relay-mcp init` and edit it."
    )


def load_config(explicit: str | os.PathLike[str] | None = None) -> Config:
    path = find_config(explicit)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e
    return parse_config(data, source=path)


def parse_config(data: dict[str, Any], source: Path | None = None) -> Config:
    base = source.parent if source else Path.cwd()

    srv = dict(data.get("server", {}))
    defaults = ServerConfig()
    db = Path(srv["db"]).expanduser() if "db" in srv else defaults.db
    if not db.is_absolute():
        db = base / db
    server = ServerConfig(
        host=str(srv.get("host", defaults.host)),
        port=int(srv.get("port", defaults.port)),
        db=db,
        max_concurrent=int(srv.get("max_concurrent", defaults.max_concurrent)),
        max_attachment_chars=int(srv.get("max_attachment_chars", defaults.max_attachment_chars)),
        retention_days=int(srv.get("retention_days", defaults.retention_days)),
        max_wait_seconds=int(srv.get("max_wait_seconds", defaults.max_wait_seconds)),
    )
    if server.max_concurrent < 1:
        raise ConfigError("[server] max_concurrent must be at least 1")

    sec = data.get("security", {})
    roots = [Path(r).expanduser() for r in sec.get("allowed_roots", [])] or [Path.home()]
    for r in roots:
        if not r.is_absolute():
            raise ConfigError(f"[security] allowed_roots must be absolute paths (got {r})")
    deny = list(DEFAULT_DENY_PATTERNS) + [str(p) for p in sec.get("deny_patterns", [])]
    security = SecurityConfig(allowed_roots=roots, deny_patterns=deny)

    backends = {name: dict(cfg) for name, cfg in data.get("backends", {}).items()}
    if not backends:
        raise ConfigError("No [backends.*] defined")
    for name, cfg in backends.items():
        kind = cfg.setdefault("type", "openai")
        if kind not in BACKEND_TYPES:
            raise ConfigError(f"[backends.{name}] type must be one of {BACKEND_TYPES}")
        required = "base_url" if kind == "openai" else "command"
        if required not in cfg:
            raise ConfigError(f"[backends.{name}] requires '{required}'")

    tiers = {name: list(dests) for name, dests in data.get("tiers", {}).items()}
    if not tiers:
        raise ConfigError("No [tiers] defined")
    for tier, dests in tiers.items():
        if not dests:
            raise ConfigError(f"[tiers] '{tier}' has no destinations")
        for dest in dests:
            backend, sep, model = dest.partition("/")
            if not sep or not model:
                raise ConfigError(f"[tiers] '{tier}': '{dest}' must look like 'backend/model'")
            if backend not in backends:
                raise ConfigError(f"[tiers] '{tier}': unknown backend '{backend}' in '{dest}'")

    transcription = data.get("transcription")
    if transcription:
        tb = transcription.get("backend")
        if tb not in backends or backends[tb]["type"] != "openai":
            raise ConfigError("[transcription] backend must be a defined 'openai'-type backend")
        if not transcription.get("model"):
            raise ConfigError("[transcription] requires 'model'")

    return Config(server, security, backends, tiers, transcription or None, source)
