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
        }
    )
    return Relay(cfg, backends={"s": Stub("s", {})}), root


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
        "delegate", "transcribe", "task_status", "list_tasks",
        "cancel_task", "list_tiers", "list_models",
    }  # fmt: skip
    assert "light" in listed
