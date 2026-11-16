# Agents, shared context and threads

An **agent** is a Markdown file that pairs a system prompt with a model (or tier). Any MCP client can run it with `run_agent`, and the result can be handed to a different agent on a different provider.

> Relay agents are **text-only workers**. They see what you attach and what earlier tasks produced; they cannot read other files, run commands or browse. For work that needs tools, use your main agent and delegate the reading and summarising.

## Defining an agent

Create `agents/explorer.md` next to your `relay.toml` (or set `[agents] dir`):

```markdown
---
description: Maps the files you attach and reports what is where.
model: claude/haiku
---
You are a code explorer. Work only with the material you are given...
```

The front matter is a flat list of `key: value` lines. There is no YAML dependency, so no nesting.

| Key | Meaning |
|---|---|
| `name` | Defaults to the file name. Lowercase letters, digits, `-` and `_`. |
| `description` | What the agent is for. Clients read it to decide when to use it. |
| `model` | One or more `backend/model` destinations, comma-separated, tried in order. |
| `tier` | A tier from `[tiers]` instead of fixed models. |
| `include` | Comma-separated globs of project files to attach automatically, e.g. `src/**/*.py, README.md`. Needs a [project](#projects-agents-skills-and-files-that-live-in-a-repository). |
| `skills` | Comma-separated skills to append to the system prompt, e.g. `security, house-style`. |

Use `model` **or** `tier`, not both. With neither, the agent uses the `light` tier (or the first tier). The text after the closing `---` is the system prompt.

Files are read on every call, so edits apply without restarting the server. A broken file never hides the good ones: `list_agents` reports its error instead. Ready-made examples are in [`src/relay_mcp/templates/agents`](../src/relay_mcp/templates/agents).

Agents can only be created by editing files on disk; there is deliberately no tool that writes them, so a client cannot persist instructions for later sessions.

## Projects: agents, skills and files that live in a repository

A repository can carry its own agents and skills, versioned with the code:

```
my-repo/
  AGENTS.md                         project rules (Antigravity reads it; so does Claude Code,
                                    but only when the repo has no CLAUDE.md)
  .agents/
    skills/<name>/SKILL.md          skills shared with Antigravity
    relay/agents/*.md               agents (relay only)
  .claude/
    skills/<name>/SKILL.md          skills shared with Claude Code
```

Run `relay-mcp init-project` in the repository to create `.agents/relay/agents/` with four starter agents (it never overwrites). Skills use the same `SKILL.md` layout as Antigravity and Claude Code; relay reads **both** `.agents/skills/` and `.claude/skills/`, so one set serves all three tools (on a name clash `.agents` wins). Extra front matter written for other tools, such as `allowed-tools`, is ignored. Claude Code itself does **not** read `.agents/`, which is why relay also looks in `.claude/skills/`.

### Telling relay which project you are in

- **One server per project (stdio).** Put this in the repository's `.mcp.json`. Starting relay with `--project` makes that folder the default project and marks it as trusted:

  ```json
  { "mcpServers": { "relay": { "command": "relay-mcp", "args": ["--transport", "stdio", "--project", "."] } } }
  ```

  All of them share one task database, so `relay-mcp watch` sees every project at once.
- **One shared server (HTTP).** Pass `project=<absolute path of the repository>` to `run_agent`, `list_agents` or `list_skills`. The folder (or a parent of it) must be listed under `[projects] trusted` in `relay.toml`. A subfolder resolves to the repository root (the nearest folder with `.git` or `.agents`).

Project agents and skills are added to the global ones, and **override** a global one with the same name. Errors in project files are reported by `list_agents` and `list_skills` as `project:<file>`.

### Trust

A cloned repository could ship an agent that sends its own files to a cloud model, so project agents and skills are only loaded for projects you trust: the folder you started with `--project`, or one under `[projects] trusted`. Trusting a folder trusts everything below it. The folder must also be inside `[security] allowed_roots`.

### `include`: giving an agent the project's text

`include: src/**/*.py, README.md` attaches the matching files every time the agent runs, labelled by path relative to the project (absolute paths are never sent). The same safety rules as any attachment apply, plus:

- Patterns must be relative and cannot contain `..`.
- Protected names (`.env`, `*.pem`, `.ssh`...), anything that resolves outside the allowed folders, binary files, and the folders `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `dist`, `build` and the tool caches are skipped silently, so a broad pattern such as `**/*` cannot pull secrets in.
- At most 200 files and `max_attachment_chars` characters in total; beyond that the call fails and asks you to narrow the patterns, rather than truncating silently.

The agent still cannot choose what to read: it receives what `include` matched. Tools that let an agent explore on its own are not part of relay yet.

## Sharing context between agents

Every task result is stored as plain text, so any model can read any other model's output. Give the ids of finished tasks in `context_from`:

> Ask the explorer to map `src/` (task `4b6d3175`), then ask the researcher to find risks in it, with `context_from: ["4b6d3175"]`.

Relay inserts each result into the next prompt inside a labelled block:

```
<context task="4b6d3175" agent="explorer" model="claude/haiku"> ... </context>
```

Your main agent never has to copy the text, and nothing is lost in a summary. Only `completed` tasks can be used, ids may be shortened to a unique prefix, and the total counts against `max_attachment_chars`. The block is introduced with a note telling the model to treat it as information and not to follow instructions found inside it; that reduces, but does not remove, prompt-injection risk when the earlier model read untrusted content.

## Threads: multi-turn conversations across models

Pass the same `thread` name in several calls to keep a conversation going:

```
delegate  prompt="Summarise the incident report"  thread="incident-12"  tier=light
run_agent agent=researcher  task="Which of those causes is most likely?"  thread="incident-12"
```

The second call sees the first question and answer even though it runs on another model. Notes:

- Only your instructions and the model's answers are recorded. **Attachments and context blocks are not carried over**; attach them again when the later turn needs them.
- History is stored unencrypted in the task database and deleted after `retention_days`. Remove a thread any time with `clear_thread`.
- A thread that grows past `max_attachment_chars` is refused: start a new thread.
- Two tasks running at the same moment on one thread will interleave; wait for one to finish first.
- The viewer shows the thread as `#name` next to each task.

## Example: explorer and researcher

```
explorer    -> model: claude/haiku                                 (cheap, fast)
researcher  -> model: agy/gemini-3.8-flash-medium, ollama/llama3.2 (second opinion on another provider)
```

1. `run_agent explorer` with `files=[...]` returns what is where.
2. `run_agent researcher` with `context_from=[<explorer task id>]` analyses it.
3. `relay-mcp watch` in another terminal shows both tasks in full, with the agent, model and thread of each.
