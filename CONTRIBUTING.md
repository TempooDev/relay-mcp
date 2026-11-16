# Contributing

Thanks for helping. Bug reports, backend recipes and small focused pull requests are all welcome.

## Setup

```bash
git clone https://github.com/TempooDev/relay-mcp
cd relay-mcp
uv sync
```

## Before opening a pull request

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
```

CI runs the same on Linux and Windows. Please add a test for any behaviour you change; the suite needs no network, no API keys and no installed model CLIs (backends are stubbed or use the Python interpreter as a fake CLI).

## Layout

| File | Responsibility |
|---|---|
| `config.py` | Load and validate the TOML file |
| `security.py` | Path allow-list, protected names, output rules |
| `backends.py` | OpenAI-compatible and CLI backends, tier routing and fallback |
| `store.py` / `tasks.py` | SQLite persistence and background execution |
| `server.py` | Application logic (`Relay`) and the MCP tools |
| `cli.py` | `relay-mcp` command |

## Guidelines

- Anything that touches the filesystem must go through `PathPolicy`.
- New backend types should implement the `Backend` interface and be listed in `config.BACKEND_TYPES`.
- Keep the default install dependency-light.
- Documentation and code are in English; translations of the README are welcome as `README.<lang>.md`.
