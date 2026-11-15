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
git clone https://github.com/OWNER/relay-mcp
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
| `task_status` | Get a task's status and result; can wait for it to finish. |
| `list_tasks` | Recent tasks, newest first. |
| `cancel_task` | Cancel a pending or running task. |
| `list_tiers` | The tiers and the models behind each. |
| `list_models` | Live list of what every backend offers, plus the tier mapping. |
| `transcribe` | Transcribe an audio file (needs a `[transcription]` section). |

Task statuses: `pending`, `running`, `completed`, `failed`, `cancelled`, `interrupted` (the server restarted mid-task).

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
- **No overwriting, no executables.** `save_to` only creates new files and refuses script/binary extensions.
- **Privacy follows the tier.** `local` stays on your machine; `light`, `heavy` and `max` send the content to the provider behind them. Tasks and results are stored unencrypted in the SQLite database (auto-deleted after `retention_days`).
- **CLI backends** run with tools disabled where the CLI allows it (`claude --tools ""`). Check how any CLI you add behaves in non-interactive mode.

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
