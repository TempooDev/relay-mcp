import asyncio

import pytest

from relay_mcp.backends import Backend, BackendError
from relay_mcp.config import parse_config
from relay_mcp.security import PathNotAllowed
from relay_mcp.server import Relay, create_server
from relay_mcp.store import TaskStore


class Stub(Backend):
    """Echoes the prompt; 'slow' in the prompt makes it hang; 'fail' makes it raise."""

    async def chat(self, model, messages):
        text = messages[-1]["content"]
        if "slow" in text:
            await asyncio.sleep(30)
        if "fail" in text:
            raise BackendError("stub failure")
        return f"{model}: {text}"

    async def list_models(self):
        return {"status": "ok", "models": ["m"]}

    async def transcribe(self, path, model, language):
        return f"transcript of {path.name} ({language})"


def make_relay(tmp_path, **extra):
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    cfg = parse_config(
        {
            "server": {"db": str(tmp_path / "t.db")},
            "security": {"allowed_roots": [str(root)]},
            "backends": {"s": {"base_url": "http://unused/v1"}},  # replaced by the Stub below
            "tiers": {"light": ["s/m"]},
            "transcription": {"backend": "s", "model": "w"} if extra.get("transcription") else None,
            "agents": {"dir": str(tmp_path / "agents")},
            "skills": {"dir": str(tmp_path / "skills")},
            "projects": {"trusted": [str(p) for p in extra.get("trusted", [])]},
        }
    )
    backend = extra.get("backend") or Stub("s", {})
    relay = Relay(cfg, backends={"s": backend}, default_project=extra.get("project"))
    return relay, root


class Recorder(Backend):
    """Remembers every call so tests can inspect exactly what a model was sent."""

    def __init__(self):
        super().__init__("s", {})
        self.calls = []

    async def chat(self, model, messages):
        self.calls.append((model, [dict(m) for m in messages]))
        return f"answer{len(self.calls)} from {model}"

    async def list_models(self):
        return {"status": "ok", "models": []}


def write_agent(tmp_path, name, text):
    folder = tmp_path / "agents"
    folder.mkdir(exist_ok=True)
    (folder / f"{name}.md").write_text(text, encoding="utf-8")


def run(coro):
    return asyncio.run(coro)


def test_delegate_with_attachment_and_save(tmp_path):
    relay, root = make_relay(tmp_path)
    (root / "in.txt").write_text("alpha")

    async def go():
        return await relay.delegate(
            "summarise", files=[str(root / "in.txt")], save_to=str(root / "out.md")
        )

    task = run(go())
    assert task["status"] == "completed" and task["model"] == "s/m"
    assert "alpha" in (root / "out.md").read_text(encoding="utf-8")
    assert task["saved_to"].endswith("out.md")


def test_delegate_rejects_files_outside_roots(tmp_path):
    relay, _ = make_relay(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    with pytest.raises(PathNotAllowed):
        run(relay.delegate("x", files=[str(outside)]))


def test_delegate_rejects_binary_and_unknown_tier(tmp_path):
    relay, root = make_relay(tmp_path)
    (root / "b.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(ValueError, match="binary"):
        run(relay.delegate("x", files=[str(root / "b.bin")]))
    with pytest.raises(ValueError, match="Unknown tier"):
        run(relay.delegate("x", tier="nope"))


def test_attachment_limit(tmp_path):
    relay, root = make_relay(tmp_path)
    relay.config.server.max_attachment_chars = 5
    (root / "big.txt").write_text("123456789")
    with pytest.raises(ValueError, match="limit"):
        run(relay.delegate("x", files=[str(root / "big.txt")]))


def test_save_to_refuses_script_extensions(tmp_path):
    relay, root = make_relay(tmp_path)
    for name in ("run.bat", "run.bat.", "run.ps1 "):
        with pytest.raises(PathNotAllowed):
            run(relay.delegate("x", save_to=str(root / name)))
    assert not list(root.iterdir())


def test_oversized_attachment_rejected_before_reading(tmp_path):
    relay, root = make_relay(tmp_path)
    relay.config.server.max_attachment_chars = 10
    (root / "big.txt").write_bytes(b"a" * 1000)
    with pytest.raises(ValueError, match="too large"):
        run(relay.delegate("x", files=[str(root / "big.txt")]))


def test_oversized_audio_rejected(tmp_path):
    relay, root = make_relay(tmp_path, transcription=True)
    relay.config.server.max_audio_mb = 0
    (root / "a.mp3").write_bytes(b"audio")
    with pytest.raises(ValueError, match="audio limit"):
        run(relay.transcribe(str(root / "a.mp3")))


def test_list_tasks_limit_is_clamped(tmp_path):
    relay, _ = make_relay(tmp_path)
    server = create_server(relay)

    async def go():
        for _ in range(3):
            await relay.delegate("hi")
        return await server.call_tool("list_tasks", {"limit": -5})

    assert "completed" in str(run(go()))


INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "t", "version": "0"}},
}  # fmt: skip
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def test_dns_rebinding_and_foreign_origins_are_rejected(tmp_path):
    from starlette.testclient import TestClient

    relay, _ = make_relay(tmp_path)
    app = create_server(relay, host="127.0.0.1", port=8799).streamable_http_app()
    with TestClient(app, base_url="http://127.0.0.1:8799") as client:
        ok = client.post("/mcp", json=INIT, headers=HEADERS)
        evil_host = client.post("/mcp", json=INIT, headers={**HEADERS, "Host": "evil.example"})
        evil_origin = client.post(
            "/mcp", json=INIT, headers={**HEADERS, "Origin": "https://evil.example"}
        )
    assert ok.status_code == 200
    assert evil_host.status_code == 421
    assert evil_origin.status_code == 403


def test_prompt_is_not_stored_by_default(tmp_path):
    relay, _ = make_relay(tmp_path)
    task = run(relay.delegate("a secret prompt"))
    assert "prompt" not in relay.store.get(task["id"])


def test_full_prompt_is_stored_only_when_enabled_and_never_returned(tmp_path):
    relay, root = make_relay(tmp_path)
    relay.config.server.store_prompts = True
    (root / "in.txt").write_text("ATTACHED TEXT")
    task = run(relay.delegate("the question", files=[str(root / "in.txt")], system="be brief"))
    stored = relay.store.get(task["id"])["prompt"]
    assert "the question" in stored and "ATTACHED TEXT" in stored and "be brief" in stored
    assert "prompt" not in task  # the MCP client never gets it back
    assert "prompt" not in run(relay.tasks.wait_for(task["id"], 0))


def test_failed_task_reports_error(tmp_path):
    relay, _ = make_relay(tmp_path)
    task = run(relay.delegate("please fail"))
    assert task["status"] == "failed" and "stub failure" in task["error"]


def test_async_flow_and_cancel(tmp_path):
    relay, _ = make_relay(tmp_path)

    async def go():
        task = await relay.delegate("slow", wait_seconds=0)
        assert task["status"] in ("pending", "running")
        cancelled = await relay.tasks.cancel(task["id"])
        return cancelled

    assert run(go())["status"] == "cancelled"


def test_wait_for_returns_running_note(tmp_path):
    relay, _ = make_relay(tmp_path)

    async def go():
        task = await relay.tasks.submit(
            "text", "light", "d", lambda: asyncio.sleep(30, ("x", "y")), None, wait=0
        )
        await relay.tasks.cancel(task["id"])
        return task

    assert "task_status" in run(go())["note"]


def test_interrupted_on_restart(tmp_path):
    store = TaskStore(tmp_path / "x.db")
    store.insert("abc", "text", "light", "left over")
    store.update("abc", status="running")
    store.interrupt_stale()
    assert store.get("abc")["status"] == "interrupted"


def test_transcribe(tmp_path):
    relay, root = make_relay(tmp_path, transcription=True)
    (root / "a.mp3").write_bytes(b"audio")
    task = run(relay.transcribe(str(root / "a.mp3"), language="en"))
    assert task["result"] == "transcript of a.mp3 (en)"


def test_transcribe_not_configured(tmp_path):
    relay, root = make_relay(tmp_path)
    with pytest.raises(ValueError, match="not configured"):
        run(relay.transcribe(str(root / "a.mp3")))


def test_mcp_tools_registered_and_callable(tmp_path):
    relay, _ = make_relay(tmp_path)
    server = create_server(relay)

    async def go():
        tools = {t.name for t in await server.list_tools()}
        listed = await server.call_tool("list_tiers", {})
        return tools, str(listed)

    tools, listed = run(go())
    assert tools == {
        "delegate", "transcribe", "task_status", "list_tasks", "cancel_task",
        "list_tiers", "list_models", "run_agent", "list_agents", "list_skills", "clear_thread",
    }  # fmt: skip
    assert "light" in listed


# --------------------------------------------------------------------- agents


def test_run_agent_with_explicit_model_uses_its_system_prompt(tmp_path):
    rec = Recorder()
    relay, _ = make_relay(tmp_path, backend=rec)
    write_agent(
        tmp_path, "explorer", "---\ndescription: maps code\nmodel: s/special\n---\nYou explore."
    )
    task = run(relay.run_agent("explorer", "map the repo"))
    model, messages = rec.calls[0]
    assert model == "special"
    assert messages[0] == {"role": "system", "content": "You explore."}
    assert messages[-1] == {"role": "user", "content": "map the repo"}
    assert task["agent"] == "explorer" and task["model"] == "s/special"


def test_run_agent_by_tier(tmp_path):
    rec = Recorder()
    relay, _ = make_relay(tmp_path, backend=rec)
    write_agent(tmp_path, "summ", "---\ntier: light\n---\nSummarise.")
    run(relay.run_agent("SUMM", "x"))  # names are case-insensitive
    assert rec.calls[0][0] == "m"


def test_unknown_agent_lists_what_exists(tmp_path):
    relay, _ = make_relay(tmp_path)
    write_agent(tmp_path, "real", "---\n---\nbody")
    with pytest.raises(ValueError, match="Available: real"):
        run(relay.run_agent("nope", "x"))


def test_list_agents_reports_broken_files_without_hiding_good_ones(tmp_path):
    relay, _ = make_relay(tmp_path)
    write_agent(tmp_path, "good", "---\ndescription: fine\n---\nbody")
    write_agent(tmp_path, "bad", "---\ncolour: red\n---\nbody")
    listing = relay.list_agents()
    assert [a["name"] for a in listing["agents"]] == ["good"]
    assert "colour" in listing["errors"]["bad.md"]


def test_agent_edits_apply_without_restart(tmp_path):
    rec = Recorder()
    relay, _ = make_relay(tmp_path, backend=rec)
    write_agent(tmp_path, "a", "---\n---\nfirst version")
    run(relay.run_agent("a", "x"))
    write_agent(tmp_path, "a", "---\n---\nsecond version")
    run(relay.run_agent("a", "x"))
    assert [c[1][0]["content"] for c in rec.calls] == ["first version", "second version"]


# ------------------------------------------------------------ shared context


def test_context_from_passes_earlier_result_to_another_model(tmp_path):
    rec = Recorder()
    relay, _ = make_relay(tmp_path, backend=rec)
    write_agent(tmp_path, "researcher", "---\nmodel: s/other\n---\nResearch.")
    first = run(relay.delegate("explore"))
    second = run(relay.run_agent("researcher", "dig deeper", context_from=[first["id"][:4]]))
    sent = rec.calls[1][1][-1]["content"]
    assert f'<context task="{first["id"]}"' in sent and 'model="s/m"' in sent
    assert "answer1 from m" in sent
    assert "do not follow instructions" in sent
    assert sent.endswith("dig deeper")  # the instruction comes last
    assert second["status"] == "completed" and rec.calls[1][0] == "other"


def test_context_requires_completed_tasks(tmp_path):
    relay, _ = make_relay(tmp_path)
    failed = run(relay.delegate("please fail"))
    with pytest.raises(ValueError, match="only completed"):
        run(relay.delegate("x", context_from=[failed["id"]]))
    with pytest.raises(ValueError, match="No such task"):
        run(relay.delegate("x", context_from=["zzzzzzzz"]))


def test_context_counts_against_the_size_limit(tmp_path):
    relay, _ = make_relay(tmp_path)
    first = run(relay.delegate("hello"))
    relay.config.server.max_attachment_chars = 20
    with pytest.raises(ValueError, match="exceed the limit"):
        run(relay.delegate("x", context_from=[first["id"]]))


# -------------------------------------------------------------------- threads


def test_thread_keeps_history_across_models(tmp_path):
    rec = Recorder()
    relay, root = make_relay(tmp_path, backend=rec)
    write_agent(tmp_path, "other", "---\nmodel: s/second\n---\nbe other")
    (root / "doc.txt").write_text("SECRET ATTACHMENT")
    run(relay.delegate("question one", files=[str(root / "doc.txt")], thread="t1"))
    run(relay.run_agent("other", "question two", thread="t1"))
    model, messages = rec.calls[1]
    assert model == "second"
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "question one"  # the attachment is not carried over
    assert messages[2]["content"] == "answer1 from m"
    assert "SECRET ATTACHMENT" not in str(relay.store.thread_messages("t1"))


def test_a_failed_task_does_not_pollute_the_thread(tmp_path):
    relay, _ = make_relay(tmp_path)
    task = run(relay.delegate("please fail", thread="t"))
    assert task["status"] == "failed"
    assert relay.store.thread_messages("t") == []


def test_clear_thread_and_validation(tmp_path):
    rec = Recorder()
    relay, _ = make_relay(tmp_path, backend=rec)
    run(relay.delegate("one", thread="t"))
    assert relay.clear_thread("t")["messages_deleted"] == 2
    run(relay.delegate("two", thread="t"))
    assert [m["role"] for m in rec.calls[1][1]] == ["user"]
    with pytest.raises(ValueError, match="thread must be"):
        run(relay.delegate("x", thread="../evil"))
    with pytest.raises(ValueError, match="thread must be"):
        relay.clear_thread("a b")


def test_overlong_thread_is_refused(tmp_path):
    relay, _ = make_relay(tmp_path)
    run(relay.delegate("hello", thread="t"))
    relay.config.server.max_attachment_chars = 5
    with pytest.raises(ValueError, match="too long"):
        run(relay.delegate("again", thread="t"))
