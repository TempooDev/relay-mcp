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

Use `model` **or** `tier`, not both. With neither, the agent uses the `light` tier (or the first tier). The text after the closing `---` is the system prompt.

Files are read on every call, so edits apply without restarting the server. A broken file never hides the good ones: `list_agents` reports its error instead. Ready-made examples are in [`examples/agents`](../examples/agents).

Agents can only be created by editing files on disk; there is deliberately no tool that writes them, so a client cannot persist instructions for later sessions.

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
