# Configuration reference

`relay-mcp` reads a single TOML file. Create a starting point with `relay-mcp init`; the packaged [example](../src/relay_mcp/relay.example.toml) documents every option inline.

**Lookup order:** `--config PATH` → `RELAY_MCP_CONFIG` → `./relay.toml` → `~/.config/relay-mcp/config.toml` (Windows: `%APPDATA%\relay-mcp\config.toml`).

The file is validated at startup. Unknown backends in a tier, malformed `backend/model` strings and missing required keys stop the server with a clear message.

## `[server]`

| Key | Default | Meaning |
|---|---|---|
| `host` | `127.0.0.1` | Interface to bind. There is no authentication, so keep it on localhost. |
| `port` | `8765` | HTTP port. Also `--port`. |
| `db` | user data folder | SQLite file. Relative paths are resolved next to the config file. |
| `max_concurrent` | `3` | Tasks that run at once; the rest wait in the queue. |
| `max_attachment_chars` | `400000` | Total size of files attached to one task. |
| `retention_days` | `30` | Finished tasks older than this are deleted at startup. `0` keeps everything. |
| `max_wait_seconds` | `300` | Upper bound for `wait_seconds` on any call. |
| `store_prompts` | `false` | Keep the full text sent to the model (system prompt and attachments included) so `relay-mcp show` can display it. Stored unencrypted; never returned to MCP clients. Off by default. |

## `[agents]`

| Key | Default | Meaning |
|---|---|---|
| `dir` | `agents` | Folder with the agent `.md` files. Relative paths are resolved next to the config file. See [agents.md](agents.md). |

## `[skills]`

| Key | Default | Meaning |
|---|---|---|
| `dir` | `skills` | Folder of global skills (`<name>/SKILL.md`). Relative paths are resolved next to the config file. Project skills are read from `.agents/skills/` and `.claude/skills/`. |

## `[projects]`

| Key | Default | Meaning |
|---|---|---|
| `trusted` | `[]` | Absolute folders whose `.agents/` agents and skills may be loaded when a client passes `project=`. A folder trusts everything below it. A project started with `--project` is trusted without being listed. See [agents.md](agents.md#projects-agents-skills-and-files-that-live-in-a-repository). |

## `[security]`

| Key | Default | Meaning |
|---|---|---|
| `allowed_roots` | your home folder | Absolute folders the server may read from and write to. |
| `deny_patterns` | `[]` | Extra names to refuse, added to the built-in list (`.ssh`, `.aws`, `.gnupg`, `.kube`, `.docker`, `.env`, `.env.*`, `*.pem`, `*.key`, `*.kdbx`, `id_rsa*`, `id_ed25519*`, `.netrc`, `.git-credentials`, `.npmrc`, `.pypirc`, `credentials*`). |
| `output_extensions` | `.md .txt .json .csv .tsv .html .htm .xml .yaml .yml .log .srt .vtt .rst .tex` | Extensions `save_to` may create. An allow-list: anything else, including no extension, is refused. |

Paths are resolved (symlinks and `..` included) before the check. On Windows, names containing `:` (NTFS alternate data streams) and names ending in a dot or space (which Windows silently strips) are refused. `save_to` only creates new files and never overwrites.

The server also keeps `[server] max_audio_mb` (default `100`) as the largest audio file `transcribe` will read.

## Backends

A backend is a named place where models run: `[backends.<name>]`. The `type` key selects the kind and defaults to `openai`.

### `type = "openai"`: OpenAI-compatible HTTP

| Key | Meaning |
|---|---|
| `base_url` | Required. Includes the `/v1` part, e.g. `http://localhost:11434/v1`. |
| `api_key_env` | Name of the environment variable that holds the key. Preferred. |
| `api_key` | The key itself. Avoid it: config files get shared. |

If `api_key_env` is set but the variable is empty, the backend is **skipped** and the next destination of the tier is tried.

```toml
[backends.ollama]
base_url = "http://localhost:11434/v1"

[backends.litellm]                       # one proxy for many providers
base_url = "http://localhost:4000/v1"
api_key_env = "LITELLM_API_KEY"

[backends.gemini]                        # Gemini API key, OpenAI-compatible endpoint
base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
api_key_env = "GEMINI_API_KEY"
```

`list_models` calls `GET {base_url}/models`, so models you add to LiteLLM or Ollama appear automatically.

### `type = "cli"`: command-line tools

Runs a command in non-interactive mode and returns its output.

| Key | Meaning |
|---|---|
| `command` | Required. Executable name, looked up on `PATH`. If it is missing the backend is skipped. |
| `args` | Argument list. `{model}` is replaced with the model of the destination. |
| `prompt_arg` | If set, the prompt is appended as `<prompt_arg>=<prompt>` instead of being sent through stdin (for tools that cannot read stdin). |
| `max_arg_chars` | Prompt size limit when `prompt_arg` is used. Default `28000`, safe for Windows. |
| `timeout` | Seconds before the process is killed. Default `600`. |
| `error_patterns` | Regular expressions (case-insensitive). If the output matches one, the call counts as a failure and the next destination is tried. For CLIs that print provider errors (content filters, quota) as normal text with exit code 0. |
| `list_args` | Command that prints available models, one per line (`id<TAB>description`). Used by `list_models`. |
| `models` | Static model list used by `list_models` when there is no `list_args`. |

Make sure the command cannot act on its own in non-interactive mode and cannot call back into relay-mcp. For Claude Code that means `--tools ""` and `--strict-mcp-config`:

```toml
[backends.claude]
type = "cli"
command = "claude"
args = ["-p", "--model", "{model}", "--tools", "", "--strict-mcp-config", "--no-session-persistence"]
models = ["opus", "sonnet", "haiku"]
```

Agent-style CLIs such as `agy` are full agents, not plain model endpoints. Even in `--mode plan` they may add chatter to the answer (for example "I have created the plan artifact") or create their own working files. Prefer an `openai`-type backend where you need clean output, and phrase prompts as "reply with only ...".

On Windows, `.cmd`/`.bat` launchers may need quoting care; prefer the real `.exe` when there is one.

Billing and terms: CLI backends use whatever account the CLI is signed into. Check that provider's terms before automating at high volume.

## `[tiers]`

```toml
[tiers]
light = ["agy/gemini-3.8-flash-low", "ollama/llama3.2"]
heavy = ["claude/sonnet"]
```

Each entry is `backend/model`; everything after the first `/` is the model name, so `ollama/library/model:tag` works. Destinations are tried in order; the first that answers wins, and the task records which one it was. Tier names are free-form: add `private`, `code`, `long-context`...

## `[transcription]` (optional)

```toml
[transcription]
backend = "litellm"      # must be an openai-type backend
model = "whisper-1"
```

Requires a server exposing `/audio/transcriptions` (LiteLLM with a Whisper model, faster-whisper-server, OpenAI...). Ollama does not.

## Running at login

relay-mcp is a plain process; use your platform's tools.

**Windows (Task Scheduler):** create a task that runs at logon with action `uv` and arguments `--directory C:\path\to\relay-mcp run relay-mcp`.

**Linux (systemd user unit):**

```ini
[Service]
WorkingDirectory=%h/relay-mcp
ExecStart=%h/.local/bin/uv run relay-mcp
Restart=on-failure
[Install]
WantedBy=default.target
```

If you set API keys through the environment, make sure the service sees them.
