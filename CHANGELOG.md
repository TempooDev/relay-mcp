# Changelog

All notable changes are documented here. Format based on [Keep a Changelog](https://keepachangelog.com/), versioning follows [SemVer](https://semver.org/).

## [0.1.0] - Unreleased

### Added
- MCP server with tools `delegate`, `task_status`, `list_tasks`, `cancel_task`, `list_tiers`, `list_models` and `transcribe`.
- Tiers with ordered fallbacks.
- OpenAI-compatible HTTP backends (Ollama, LiteLLM, ...) and CLI backends (`claude -p`, `agy -p`).
- Persistent SQLite task queue with cancellation and restart recovery.
- Folder allow-list, protected file names, no-overwrite output policy with an extension allow-list.
- Explicit DNS-rebinding protection on the HTTP transport; warning when binding beyond localhost.
- Size limits for attachments and audio, and a cap on `list_tasks`.
- Task viewer: `relay-mcp tasks`, `show` and `watch` read the queue live and in full; optional `store_prompts` keeps the full prompt.
- `relay-mcp init` to generate a config; Streamable HTTP and stdio transports.
