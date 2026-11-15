"""Command-line entry point: `relay-mcp`."""

from __future__ import annotations

import argparse
import sys
from importlib import resources
from pathlib import Path

from . import __version__
from .config import CONFIG_NAME, ConfigError, load_config


def _init(target: Path) -> int:
    if target.exists():
        print(f"{target} already exists; not overwriting.", file=sys.stderr)
        return 1
    example = (
        resources.files("relay_mcp").joinpath("relay.example.toml").read_text(encoding="utf-8")
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(example, encoding="utf-8")
    print(f"Wrote {target}. Edit it, then run: relay-mcp")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="relay-mcp", description="MCP server that delegates tasks to other models."
    )
    parser.add_argument("--version", action="version", version=f"relay-mcp {__version__}")
    parser.add_argument("-c", "--config", help="path to the config file (default: search)")
    parser.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default="http",
        help="http: one shared server (default). stdio: one process per client.",
    )
    parser.add_argument("--host", help="override [server] host")
    parser.add_argument("--port", type=int, help="override [server] port")
    sub = parser.add_subparsers(dest="command")
    init = sub.add_parser("init", help=f"write an example {CONFIG_NAME} to edit")
    init.add_argument("path", nargs="?", default=CONFIG_NAME, help=f"default: ./{CONFIG_NAME}")
    args = parser.parse_args(argv)

    if args.command == "init":
        return _init(Path(args.path))

    from .server import Relay, create_server  # imported late: keeps `--help` and `init` fast

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    relay = Relay(config)
    server = create_server(relay, host=args.host, port=args.port)
    if args.transport == "http":
        print(
            f"relay-mcp {__version__} using {config.source} - "
            f"http://{args.host or config.server.host}:{args.port or config.server.port}/mcp",
            file=sys.stderr,
        )
        server.run(transport="streamable-http")
    else:
        server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
