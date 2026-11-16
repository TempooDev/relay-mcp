# relay-mcp

**Let your AI agents hand work to other models.** `relay-mcp` is an [MCP](https://modelcontextprotocol.io) server that gives Claude Code, Antigravity, Gemini CLI, and any other MCP client one shared tool for delegating tasks to cheaper, faster or local models, through a persistent task queue.

> English · [Español](README.es.md)

```
Claude Code ──┐                          ┌─► Gemini (via Antigravity CLI)   "light"
Antigravity ──┼─► relay-mcp ─ queue ─────┼─► Ollama / LiteLLM / any API     "local"
Gemini CLI  ──┘   (one server, SQLite)   ├─► Claude Sonnet                  "heavy"
                                         └─► Claude Fable / Opus            "max"
```

## Why

- **Spend premium tokens only where they matter.** Let your best model plan, and send the bulk work (summaries, classification, extraction, first drafts) to something cheaper or free.
- **Keep sensitive material on your machine.** Route a task to a `local` tier and nothing leaves your computer.
- **One setup for every client.** Claude Code and Antigravity share the same server, the same queue and the same configuration.
- **Long jobs do not hit MCP timeouts.** Slow tasks return an id; poll for the result later, from any client.

## Features

- Tiers instead of model names: ask for `light` or `heavy`, and change what is behind them in one config file.
- Ordered fallbacks per tier: if the first destination is down or missing a key, the next one answers.
- Two backend types:
  - **OpenAI-compatible HTTP**: Ollama, LiteLLM, vLLM, LM Studio, OpenAI, Gemini's OpenAI endpoint...
  - **CLI**: `claude -p`, `agy -p`, or any non-interactive command, using the account you are already signed in with.
- Persistent SQLite queue: status, results, cancel, history that survives restarts.
- Pass file **paths**, not contents: the server reads the files itself, so you do not pay for those tokens twice.
- Optional audio transcription through any OpenAI-compatible `/audio/transcriptions` endpoint.
- Secure by default: localhost only, folder allow-list, protected file names, no overwriting. See [Security](#security).

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/) (recommended).
- At least one backend: [Ollama](https://ollama.com), an API key, or a CLI such as `claude` or `agy`.

## Quick start

```bash
git clone https://github.com/TempooDev/relay-mcp
cd relay-mcp
uv sync
uv run relay-mcp init        # writes ./relay.toml
```

Edit `relay.toml`: set the models you actually have (see [Configuration](#configuration)). Then start the server and leave it running:

```bash
uv run relay-mcp
# relay-mcp 0.1.0 using ./relay.toml - http://127.0.0.1:8765/mcp
```

### Connect your clients

**Claude Code**

```bash
claude mcp add --transport http --scope user relay http://127.0.0.1:8765/mcp
```

**Antigravity**: add to `~/.gemini/config/mcp_config.json` (or run `agy mcp add`):

```json
{ "mcpServers": { "relay": { "url": "http://127.0.0.1:8765/mcp" } } }
```

**Gemini CLI**: add to `~/.gemini/settings.json` (check the MCP section of your version's docs if the key differs):

```json
{ "mcpServers": { "relay": { "httpUrl": "http://127.0.0.1:8765/mcp" } } }
```

**Any other client**: point it at `http://127.0.0.1:8765/mcp` (Streamable HTTP). Clients that only launch local commands can use stdio instead; each client then gets its own process:

```json
{ "mcpServers": { "relay": { "command": "uv", "args": ["--directory", "/path/to/relay-mcp", "run", "relay-mcp", "--transport", "stdio"] } } }
```

Restart the client (or open a new session) so it picks up the server, and check it shows up (`/mcp` in Claude Code).

## Using it

You do not call the tools yourself; you ask your agent to use them:

> *"Use relay to summarise `C:\notes\meeting.txt` in five bullets and save it to `C:\notes\meeting-summary.md`."*
>
> *"Delegate the classification of these three files to the `local` tier and tell me only the categories."*
>
> *"Use relay with the `max` tier to review this design."*
>
> *"Start that summary in the background and give me the task id."*

Agents often handle small tasks themselves. If you want something always delegated, say so in the prompt or in your `CLAUDE.md` / `AGENTS.md`.

### Tools

| Tool | What it does |
|---|---|
| `delegate` | Send a text task to a tier, with optional file attachments, system prompt, and `save_to`. Waits up to `wait_seconds` (default 45), then returns an id if still running. |
| `run_agent` | Run a predefined [agent](docs/agents.md): a Markdown file with a system prompt and a model. Same options as `delegate`. |
| `list_agents` | The available agents and the model each one uses; pass `project` to include a repository's own. |
| `list_skills` | The available skills (reusable instructions agents can opt into). |
| `clear_thread` | Forget the history of a conversation thread. |
| `get_usage` | Tokens and cost spent by delegated tasks, grouped by tier, model, agent or day. |
| `task_status` | Get a task's status and result; can wait for it to finish. |
| `list_tasks` | Recent tasks, newest first. |
| `cancel_task` | Cancel a pending or running task. |
| `list_tiers` | The tiers and the models behind each. |
| `list_models` | Live list of what every backend offers, plus the tier mapping. |
| `transcribe` | Transcribe an audio file (needs a `[transcription]` section). |

Task statuses: `pending`, `running`, `completed`, `failed`, `cancelled`, `interrupted` (the server restarted mid-task).

### Agents, shared context and threads

Define agents as Markdown files, pair each with a model, and let them build on each other's work, even across providers:

```markdown
<!-- agents/explorer.md -->
---
description: Maps the files you attach and reports what is where.
model: claude/haiku
---
You are a code explorer. Work only with the material you are given...
```

> *"Run the explorer on `src/`, then the researcher with `context_from` the explorer's task, and keep going in the thread `review-1`."*

- `context_from` passes the result of earlier tasks into a new one, whichever model produced them. Your main agent never copies the text.
- `thread` keeps a multi-turn conversation going, and a later turn can use a different model.
- Examples to copy are in [`src/relay_mcp/templates/agents`](src/relay_mcp/templates/agents); the full guide is [docs/agents.md](docs/agents.md).

Relay agents are text-only workers: they see what you attach, what earlier tasks produced and what their `include` globs match, and cannot choose other files to read or run commands.

**Inside a repository.** Run `relay-mcp init-project` to create `.agents/relay/agents/` with starter agents, and add a `.mcp.json` so each repository gets a relay with its own agents and skills:

```json
{ "mcpServers": { "relay": { "command": "relay-mcp", "args": ["--transport", "stdio", "--project", "."] } } }
```

An agent can attach project files (`include: src/**/*.py, README.md`) and opt into skills (`skills: security`). Skills use the same `SKILL.md` layout as Antigravity and Claude Code and are read from both `.agents/skills/` and `.claude/skills/`. A project's agents and skills load only for trusted projects. Details in [docs/agents.md](docs/agents.md#projects-agents-skills-and-files-that-live-in-a-repository).

### See what every model is doing

Your agent summarises what comes back from a delegated task, and sometimes hides detail. To see the real thing, open a second terminal (or a pane in tmux or [herdr](https://github.com/ogulcancelik/herdr)) and follow the queue live:

```bash
relay-mcp watch          # announces each task, then prints it in full when it finishes
relay-mcp tasks          # list recent tasks
relay-mcp show 4b6d      # one task in full (the start of the id is enough)
```

The viewer reads the database directly and never writes, so it works while the server runs and also after it stops. By default it shows the first 200 characters of each prompt. To keep the **full** text sent to the model (system prompt and attached files included), set `store_prompts = true` under `[server]`. That is off by default because it stores your files' contents unencrypted; the MCP client never receives it back.

### Know what it costs

Every task records its tokens, and its cost when the backend reports one. See the totals, or see one task:

```bash
relay-mcp usage                      # last 7 days, by model
relay-mcp usage --since 24h --by tier   # also: agent, day
relay-mcp show 4b6d                  # includes the task's own usage line
```

Your agent can check before delegating with the `get_usage` tool. Tokens appear as `in>out` in `relay-mcp tasks` and `watch`, with `~` when they are estimates.

- **Exact when reported.** OpenAI-compatible backends (Ollama, LiteLLM...) report exact tokens. For `claude -p` and `agy -p`, set `output = "json"` on the backend so relay reads them from `--output-format json`; `claude` also reports a cost at list price (what your subscription is charged may differ), `agy` reports none. Without that, relay estimates about 4 characters per token and marks it.
- **Grouped by model by default**, because a tier can end up on different models (a fallback answers when the first destination fails). Tasks that ran before usage tracking existed have no data; the report tells you how many.
- **Failed attempts count.** If a destination fails and the next one answers (for example a filtered Gemini call that falls back to Ollama), the tokens the failed attempt consumed are reported as `wasted`.
- **CLI backends have a fixed overhead.** In a measurement with a trivial prompt, one `claude -p` call processed 3.5k to 7.5k input tokens and one `agy -p` call about 10k, for an answer of a few tokens. For very small tasks, delegating through a CLI can cost more than doing the task in place; it pays off on larger inputs. Backends that call an API directly have no such overhead.

## Configuration

`relay-mcp` looks for its config in this order: `--config PATH`, the `RELAY_MCP_CONFIG` environment variable, `./relay.toml`, then `~/.config/relay-mcp/config.toml` (`%APPDATA%\relay-mcp\config.toml` on Windows). The fully commented [example](src/relay_mcp/relay.example.toml) is the reference; the essentials:

```toml
[backends.ollama]                       # any OpenAI-compatible endpoint
base_url = "http://localhost:11434/v1"

[backends.claude]                       # a CLI used in non-interactive mode
type = "cli"
command = "claude"
args = ["-p", "--model", "{model}", "--tools", "", "--strict-mcp-config"]

[tiers]                                 # "backend/model", first that answers wins
light = ["ollama/llama3.2"]
heavy = ["claude/sonnet"]
```

More in [docs/configuration.md](docs/configuration.md): all options, LiteLLM and Gemini API setups, CLI backends, transcription and running at login.

## Security

`relay-mcp` reads files and sends their contents to the model behind the tier you choose. Understand that before you expose it to an agent that reads untrusted content.

- **Localhost only, no authentication.** Anyone who can reach the port can use it. Do not bind it to a public interface.
- **Folder allow-list.** Only files under `allowed_roots` (default: your home folder) can be read or written. Narrow it to your project folders.
- **Protected names** such as `.ssh`, `.aws`, `.env`, `*.pem` and `id_rsa*` are always refused.
- **No overwriting, text formats only.** `save_to` only creates new files, and only with an allow-listed extension (`.md`, `.txt`, `.json`, `.csv`...). Scripts and executables cannot be written, including Windows tricks like `run.bat.` or `run.bat::$DATA`.
- **Browser-proof.** The HTTP endpoint rejects foreign `Host` and `Origin` headers (DNS-rebinding protection), so a web page cannot reach it through your browser.
- **Privacy follows the tier.** `local` stays on your machine; `light`, `heavy` and `max` send the content to the provider behind them. Tasks and results are stored unencrypted in the SQLite database (auto-deleted after `retention_days`).
- **CLI backends** run with tools disabled where the CLI allows it (`claude --tools ""`). Agent-style CLIs such as `agy` are full agents: a file you attach could carry instructions aimed at them, and they may be able to call tools, including relay-mcp itself if it is registered globally. Headless mode auto-denies tools that need permission, but check how each CLI you add behaves, and prefer `openai`-type backends for untrusted content.

Report vulnerabilities as described in [SECURITY.md](SECURITY.md).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `error while attempting to bind on address ... 8765` | Another instance is already running. Stop it, or use `--port`. |
| Browsing to `/mcp` gives `406 Not Acceptable` | Normal: the endpoint only speaks MCP. |
| A tier fails with `skipped (... not found on PATH)` | That CLI is not installed or not on PATH; the next destination is tried. |
| A tier fails with `skipped (environment variable X is not set)` | Export the variable before starting the server. |
| `agy` tier errors on big prompts | `agy` takes the prompt as an argument; Windows limits its size. Use another backend for large inputs. |
| Client does not list the tools | Restart the client or open a new session; check the server is running. |
| `Error in standalone SSE writer` in the log | Harmless noise from the MCP SDK when a client disconnects. |

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

See [CONTRIBUTING.md](CONTRIBUTING.md). The code is small on purpose: `config.py` (validation), `security.py` (path policy), `backends.py` (HTTP and CLI backends plus routing), `store.py` and `tasks.py` (queue), `server.py` (MCP tools).

## License

[MIT](LICENSE)
