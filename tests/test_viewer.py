import io
import sqlite3

import pytest

from relay_mcp import viewer
from relay_mcp.cli import main
from relay_mcp.store import TaskStore

PLAIN = viewer.Style(color=False)


def make_store(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    store.insert("aaaa1111", "text", "light", "summarise the minutes", prompt="FULL PROMPT TEXT")
    store.update(
        "aaaa1111",
        status="completed",
        model="agy/flash",
        result="THE SUMMARY",
        finished="2099-01-01T00:00:09",
    )
    store.insert("bbbb2222", "text", "heavy", "review the design")
    store.update("bbbb2222", status="failed", error="RuntimeError: backend down")
    return store


def test_list_shows_recent_tasks(tmp_path):
    make_store(tmp_path)
    out = io.StringIO()
    viewer.list_tasks(tmp_path / "tasks.db", 10, out, PLAIN)
    text = out.getvalue()
    assert "aaaa1111" in text and "bbbb2222" in text
    assert "light -> agy/flash" in text
    assert "failed" in text


def test_show_prints_prompt_result_and_prefix_lookup(tmp_path):
    make_store(tmp_path)
    out = io.StringIO()
    viewer.show_task(tmp_path / "tasks.db", "aaaa", out, PLAIN)
    text = out.getvalue()
    assert "FULL PROMPT TEXT" in text and "THE SUMMARY" in text


def test_show_hints_when_prompt_not_stored(tmp_path):
    make_store(tmp_path)
    out = io.StringIO()
    viewer.show_task(tmp_path / "tasks.db", "bbbb", out, PLAIN)
    text = out.getvalue()
    assert "store_prompts" in text and "review the design" in text
    assert "backend down" in text


def test_show_errors(tmp_path):
    make_store(tmp_path)
    path = tmp_path / "tasks.db"
    with pytest.raises(viewer.ViewerError, match="No task starts with"):
        viewer.show_task(path, "zzzz", io.StringIO(), PLAIN)
    with pytest.raises(viewer.ViewerError, match="more than one"):
        viewer.show_task(path, "", io.StringIO(), PLAIN)


def test_prefix_wildcards_are_escaped(tmp_path):
    make_store(tmp_path)
    with pytest.raises(viewer.ViewerError, match="No task starts with"):
        viewer.show_task(tmp_path / "tasks.db", "%", io.StringIO(), PLAIN)


def test_missing_database(tmp_path):
    with pytest.raises(viewer.ViewerError, match="No task database"):
        viewer.list_tasks(tmp_path / "nope.db", 5, io.StringIO(), PLAIN)


def test_not_a_relay_database(tmp_path):
    path = tmp_path / "other.db"
    sqlite3.connect(path).execute("CREATE TABLE x (a)").connection.close()
    with pytest.raises(viewer.ViewerError, match="not a relay-mcp"):
        viewer.list_tasks(path, 5, io.StringIO(), PLAIN)


def test_watch_announces_new_tasks_and_prints_finished_ones(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    store.insert("old00000", "text", "light", "already running")
    store.update("old00000", status="running")
    steps = iter(["tick1", "tick2"])

    def fake_sleep(_seconds):
        step = next(steps)
        if step == "tick1":  # the running task ends and a new one appears
            store.update("old00000", status="completed", model="m/1", result="OLD RESULT")
            store.insert("new11111", "text", "heavy", "a brand new task")
            store.update("new11111", status="running")
        else:
            store.update("new11111", status="completed", model="m/2", result="NEW RESULT")

    out = io.StringIO()
    viewer.watch(tmp_path / "tasks.db", out, PLAIN, interval=0, max_ticks=2, sleep=fake_sleep)
    text = out.getvalue()
    assert "OLD RESULT" in text and "NEW RESULT" in text
    assert text.index("OLD RESULT") < text.index("a brand new task") < text.index("NEW RESULT")
    assert text.count("OLD RESULT") == 1  # printed once, not on every tick


def test_cli_tasks_and_show(tmp_path, capsys):
    make_store(tmp_path)
    db = str(tmp_path / "tasks.db")
    assert main(["tasks", "--db", db]) == 0
    assert "aaaa1111" in capsys.readouterr().out
    assert main(["show", "aaaa", "--db", db]) == 0
    assert "THE SUMMARY" in capsys.readouterr().out


def test_cli_reports_errors_without_traceback(tmp_path, capsys):
    assert main(["show", "x", "--db", str(tmp_path / "missing.db")]) == 1
    assert "No task database" in capsys.readouterr().err


@pytest.mark.parametrize("transport", ["http", "stdio"])
def test_ctrl_c_stops_the_server_quietly(tmp_path, capsys, monkeypatch, transport):
    """Stopping a foreground server with Ctrl+C must not print a traceback."""
    from relay_mcp import server as server_module

    config = tmp_path / "relay.toml"
    config.write_text(
        '[server]\ndb = "tasks.db"\n[backends.a]\nbase_url = "http://x/v1"\n'
        '[tiers]\nlight = ["a/m"]\n',
        encoding="utf-8",
    )
    started = []

    class FakeServer:
        def run(self, transport):
            started.append(transport)
            raise KeyboardInterrupt

    monkeypatch.setattr(server_module, "create_server", lambda *args, **kwargs: FakeServer())
    assert main(["-c", str(config), "--transport", transport]) == 0
    err = capsys.readouterr().err
    assert "relay-mcp stopped" in err and "Traceback" not in err
    assert started == ["streamable-http" if transport == "http" else "stdio"]


def test_store_migrates_database_from_earlier_version(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE tasks (id TEXT PRIMARY KEY, kind TEXT, tier TEXT, status TEXT,
        description TEXT, model TEXT, result TEXT, error TEXT, saved_to TEXT,
        created TEXT, finished TEXT)"""
    )
    db.commit()
    db.close()
    store = TaskStore(path)
    store.insert("abc12345", "text", "light", "d", prompt="p", agent="explorer", thread="t1")
    task = store.get("abc12345")
    assert (task["prompt"], task["agent"], task["thread"]) == ("p", "explorer", "t1")
    store.append_turns("t1", "q", "a")  # the thread table is created on upgrade too
    assert [m["role"] for m in store.thread_messages("t1")] == ["user", "assistant"]


def test_store_find_by_prefix(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    store.insert("abcd1111", "text", "light", "one")
    store.insert("abce2222", "text", "light", "two")
    assert store.find("abcd")["id"] == "abcd1111"
    assert store.find("abcd1111")["id"] == "abcd1111"
    with pytest.raises(ValueError, match="more than one"):
        store.find("abc")
    with pytest.raises(ValueError, match="No such task"):
        store.find("zzz")
    with pytest.raises(ValueError, match="No such task"):
        store.find("")


def test_viewer_route_shows_agent_tier_and_thread():
    task = {"tier": "light", "agent": "explorer", "thread": "t1", "model": "claude/haiku"}
    assert viewer.route(task) == "explorer@light #t1 -> claude/haiku"
    assert viewer.route({"tier": "-", "agent": "x"}) == "x"
    assert viewer.route({"tier": "heavy"}) == "heavy"
