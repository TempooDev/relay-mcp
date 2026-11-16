"""Command-line entry point: `relay-mcp`."""

from __future__ import annotations

import argparse
import sys
from importlib import resources
from pathlib import Path

from . import __version__
from .config import CONFIG_NAME, ConfigError, load_config, user_data_dir


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


MCP_JSON_SNIPPET = """{
  "mcpServers": {
    "relay": {
      "command": "relay-mcp",
      "args": ["--transport", "stdio", "--project", "."]
    }
  }
}"""


def _init_project(target: Path) -> int:
    """Scaffold `<repo>/.agents/relay/agents` with the starter agents."""
    if not target.is_dir():
        print(f"{target} is not a folder.", file=sys.stderr)
        return 1
    dest = target / ".agents" / "relay" / "agents"
    dest.mkdir(parents=True, exist_ok=True)
    created, kept = [], []
    for template in resources.files("relay_mcp").joinpath("templates", "agents").iterdir():
        out = dest / template.name
        if out.exists():
            kept.append(template.name)
            continue
        out.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        created.append(template.name)
    print(f"Agents folder: {dest}")
    for name in created:
        print(f"  created {name}")
    for name in kept:
        print(f"  kept    {name} (already exists)")
    print(
        "\nNext steps:\n"
        "  1. Check the model: lines in the agents match backends you have in relay.toml.\n"
        "  2. Add this repository to [projects] trusted in relay.toml (shared HTTP server), or\n"
        "     let a stdio server start with --project, which marks it as trusted:\n\n"
        + MCP_JSON_SNIPPET
        + "\n\n(put it in .mcp.json in the repository root)"
    )
    return 0


def _db_path(args: argparse.Namespace) -> Path:
    """Database to view: --db, else the one in the config, else the default location."""
    if args.db:
        return Path(args.db).expanduser()
    explicit = getattr(args, "config", None)
    try:
        return load_config(explicit).server.db
    except ConfigError:
        if explicit:
            raise
        return user_data_dir() / "tasks.db"


def _view(args: argparse.Namespace) -> int:
    from . import viewer

    viewer.configure_output()
    style = viewer.auto_style(sys.stdout)
    try:
        path = _db_path(args)
        if args.command == "tasks":
            viewer.list_tasks(path, args.limit, sys.stdout, style)
        elif args.command == "usage":
            viewer.show_usage(path, args.since, args.by, sys.stdout, style)
        elif args.command == "show":
            viewer.show_task(path, args.id, sys.stdout, style)
        else:
            viewer.watch(path, sys.stdout, style, interval=args.interval, limit=args.limit)
    except (viewer.ViewerError, ConfigError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
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
    parser.add_argument(
        "--project",
        help="repository this server works in: its .agents/ agents and skills are used by default "
        "(starting relay with --project marks that folder as trusted)",
    )

    sub = parser.add_subparsers(dest="command")
    init = sub.add_parser("init", help=f"write an example {CONFIG_NAME} to edit")
    init.add_argument("path", nargs="?", default=CONFIG_NAME, help=f"default: ./{CONFIG_NAME}")
    init_project = sub.add_parser(
        "init-project", help="create .agents/relay/agents with starter agents in a repository"
    )
    init_project.add_argument("path", nargs="?", default=".", help="repository folder (default: .)")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS, help="path to the config file")
    common.add_argument("--db", help="task database to read (default: the one in the config)")
    tasks = sub.add_parser("tasks", parents=[common], help="list recent tasks")
    tasks.add_argument("-n", "--limit", type=int, default=20, help="how many (default 20)")
    usage = sub.add_parser("usage", parents=[common], help="token and cost totals")
    usage.add_argument("--since", default="7d", help="period: 24h, 7d, 2w or all (default 7d)")
    usage.add_argument(
        "--by",
        choices=["tier", "model", "agent", "day"],
        default="model",
        help="group by (default model: what each model consumed, whatever tier asked for it)",
    )
    show = sub.add_parser("show", parents=[common], help="show one task in full")
    show.add_argument("id", help="task id, or the start of it")
    watch = sub.add_parser("watch", parents=[common], help="follow tasks live, in full")
    watch.add_argument("-n", "--limit", type=int, default=10, help="recent tasks to list first")
    watch.add_argument("--interval", type=float, default=1.0, help="seconds between checks")
    args = parser.parse_args(argv)

    if args.command == "init":
        return _init(Path(args.path))
    if args.command == "init-project":
        return _init_project(Path(args.path))
    if args.command in ("tasks", "show", "watch", "usage"):
        return _view(args)

    from .server import Relay, create_server  # imported late: keeps `--help` and `init` fast

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    bind = args.host or config.server.host
    if args.transport == "http" and bind not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"WARNING: listening on {bind}. relay-mcp has no authentication: anyone who can "
            "reach this port can read your allowed folders and spend your model quota.",
            file=sys.stderr,
        )
    try:
        project = Path(args.project).expanduser().resolve() if args.project else None
        relay = Relay(config, default_project=project)
    except (ValueError, PermissionError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    if relay.default_project:
        print(f"Project: {relay.default_project}", file=sys.stderr)
    server = create_server(relay, host=args.host, port=args.port)
    if args.transport == "http":
        print(
            f"relay-mcp {__version__} using {config.source} - "
            f"http://{args.host or config.server.host}:{args.port or config.server.port}/mcp",
            file=sys.stderr,
        )
        print("Follow tasks live in another terminal with: relay-mcp watch", file=sys.stderr)
    try:
        server.run(transport="streamable-http" if args.transport == "http" else "stdio")
    except KeyboardInterrupt:  # Ctrl+C is the normal way to stop a foreground server
        print("\nrelay-mcp stopped.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
