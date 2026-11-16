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
- Token and cost accounting: every task records input, output and cached tokens, cost when reported, attempts and tokens wasted by failed attempts; `relay-mcp usage`, tokens in `tasks`/`watch`/`show`, and a `get_usage` tool. CLI backends read usage from `--output-format json` with `output = "json"`.
- Project scope: agents in `<repo>/.agents/relay/agents/`, skills from `.agents/skills/` and `.claude/skills/` (`SKILL.md`), `include` globs to attach project files, `skills:` in agents, `list_skills`, `project` parameter, `--project` flag, `[projects] trusted`, and `relay-mcp init-project`.
- Agents defined as Markdown files (`run_agent`, `list_agents`), each with a system prompt and a model or tier; edits apply without restarting.
- `context_from` passes earlier task results to a new task across models, and `thread` keeps multi-turn conversations across models (`clear_thread` to forget).
- `error_patterns` on CLI backends: provider errors printed with exit code 0 count as failures, so fallbacks run and nothing bogus is stored as an answer.
- Task viewer: `relay-mcp tasks`, `show` and `watch` read the queue live and in full; optional `store_prompts` keeps the full prompt.
- `relay-mcp init` to generate a config; Streamable HTTP and stdio transports.
