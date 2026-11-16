"""Token and cost accounting: what backends report, what failed attempts waste, what is shown."""

import asyncio
import io
import json
import sqlite3
import sys
from datetime import datetime, timedelta

import httpx
import pytest

from relay_mcp import viewer
from relay_mcp.backends import (
    AllFailed,
    Backend,
    BackendError,
    CliBackend,
    OpenAIBackend,
    Reply,
    Router,
    Unavailable,
    Usage,
    estimate_tokens,
    usage_from_cli_json,
    usage_from_openai,
)
from relay_mcp.cli import main
from relay_mcp.config import ConfigError, parse_config
from relay_mcp.store import TaskStore
from test_server import make_relay

MSG = [{"role": "user", "content": "hello world"}]
PLAIN = viewer.Style(color=False)


def run(coro):
    return asyncio.run(coro)


def cli(script, **extra):
    return CliBackend(
        "c", {"type": "cli", "command": sys.executable, "args": ["-c", script], **extra}
    )


CLAUDE_JSON = (
    "import json;print(json.dumps({'type':'result','is_error':False,'result':'ok',"
    "'total_cost_usd':0.0015,'usage':{'input_tokens':2,'cache_creation_input_tokens':7503,"
    "'cache_read_input_tokens':0,'output_tokens':4}}))"
)
AGY_JSON = (
    "import json;print(json.dumps({'status':'SUCCESS','response':'ok\\n','usage':"
    "{'input_tokens':9973,'output_tokens':1,'cache_read_tokens':0,'total_tokens':9974}}))"
)


# ------------------------------------------------------------------- parsing


def test_openai_usage_is_read_including_cached_tokens():
    usage = usage_from_openai(
        {
            "prompt_tokens": 30,
            "completion_tokens": 2,
            "prompt_tokens_details": {"cached_tokens": 29},
        }
    )
    assert (usage.input_tokens, usage.output_tokens, usage.cache_tokens) == (30, 2, 29)
    assert usage.cost_usd is None and usage.estimated is False
    assert usage_from_openai(None) is None and usage_from_openai("x") is None
    assert usage_from_openai({}).total == 0


def test_claude_cli_usage_adds_cache_tokens_back_to_the_input():
    usage = usage_from_cli_json(
        {
            "input_tokens": 2,
            "cache_creation_input_tokens": 7503,
            "cache_read_input_tokens": 10,
            "output_tokens": 4,
        },
        0.0015,
    )
    assert usage.input_tokens == 2 + 7503 + 10  # everything the model processed
    assert (usage.cache_tokens, usage.output_tokens, usage.cost_usd) == (7513, 4, 0.0015)


def test_agy_cli_usage_has_no_cost():
    usage = usage_from_cli_json({"input_tokens": 9973, "output_tokens": 1, "cache_read_tokens": 3})
    assert (usage.input_tokens, usage.output_tokens, usage.cache_tokens) == (9973, 1, 3)
    assert usage.cost_usd is None


def test_usage_addition_keeps_unknown_costs_unknown():
    a, b = Usage(10, 1, cost_usd=None), Usage(5, 2, cost_usd=None)
    assert (a + b).cost_usd is None and (a + b).total == 18
    assert (a + Usage(1, 1, cost_usd=0.5)).cost_usd == 0.5
    assert (a + Usage(estimated=True)).estimated is True


def test_estimate_is_about_four_characters_per_token():
    assert (
        estimate_tokens("") == 0 and estimate_tokens("abcd") == 1 and estimate_tokens("abcde") == 2
    )


# ------------------------------------------------------------------ backends


def test_openai_backend_returns_the_reported_usage():
    def handler(request):
        body = {
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        }
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reply = run(OpenAIBackend("o", {"base_url": "http://x/v1"}, client=client).chat("m", MSG))
    assert reply.text == "hi" and (reply.usage.input_tokens, reply.usage.output_tokens) == (7, 3)


def test_cli_json_output_gets_the_flag_and_reports_claude_usage():
    script = (
        "import json,sys;print(json.dumps({'result':' '.join(sys.argv[1:]),"
        "'usage':{'input_tokens':5,'output_tokens':2},'total_cost_usd':0.01}))"
    )
    reply = run(cli(script, output="json").chat("m", MSG))
    assert "--output-format json" in reply.text  # appended by relay
    assert (reply.usage.input_tokens, reply.usage.cost_usd) == (5, 0.01)


def test_cli_json_parses_claude_and_agy_shapes():
    claude = run(cli(CLAUDE_JSON, output="json").chat("m", MSG))
    assert claude.text == "ok" and claude.usage.input_tokens == 7505 and claude.usage.cost_usd
    agy = run(cli(AGY_JSON, output="json").chat("m", MSG))
    assert agy.text == "ok" and agy.usage.input_tokens == 9973 and agy.usage.cost_usd is None


def test_json_args_can_be_customised():
    script = "import json,sys;print(json.dumps({'result':' '.join(sys.argv[1:])}))"
    reply = run(cli(script, output="json", json_args=["--format", "j"]).chat("m", MSG))
    assert reply.text == "--format j"


def test_cli_that_ignores_the_json_flag_still_works_with_estimated_usage():
    reply = run(cli("print('just text')", output="json").chat("m", MSG))
    assert reply.text == "just text" and reply.usage is None  # the router estimates it


@pytest.mark.parametrize(
    "payload",
    [
        "{'is_error':True,'result':'boom','usage':{'input_tokens':5,'output_tokens':0}}",
        "{'status':'ERROR','response':'boom','usage':{'input_tokens':5,'output_tokens':0}}",
    ],
)
def test_cli_error_reported_in_json_keeps_the_usage_it_cost(payload):
    with pytest.raises(BackendError, match="reported an error") as exc:
        run(cli(f"import json;print(json.dumps({payload}))", output="json").chat("m", MSG))
    assert exc.value.usage.input_tokens == 5


def test_error_pattern_hit_in_json_output_still_counts_the_tokens():
    script = (
        "import json;print(json.dumps({'status':'SUCCESS','response':'Blocked by filters',"
        "'usage':{'input_tokens':9000,'output_tokens':1}}))"
    )
    backend = cli(script, output="json", error_patterns=["blocked by"])
    with pytest.raises(BackendError) as exc:
        run(backend.chat("m", MSG))
    assert exc.value.usage.input_tokens == 9000


# -------------------------------------------------------------------- router


class Scripted(Backend):
    """Answers or fails as told, with the usage it was given."""

    def __init__(self, reply=None, error=None):
        super().__init__("s", {})
        self.reply, self.error = reply, error

    async def chat(self, model, messages):
        if self.error:
            raise self.error
        return self.reply

    async def list_models(self):
        return {"status": "ok", "models": []}


def test_router_estimates_usage_when_the_backend_reports_none():
    router = Router({"s": Scripted(Reply("an answer"))}, {"t": ["s/m"]})
    done = run(router.chat("t", MSG))
    assert done.usage.estimated is True and done.usage.input_tokens > 0
    assert done.usage.output_tokens == estimate_tokens("an answer")
    plain = Router({"s": Scripted("just a string")}, {"t": ["s/m"]})  # test doubles return str
    assert run(plain.chat("t", MSG)).usage.estimated is True


def test_router_keeps_reported_usage_exact():
    router = Router({"s": Scripted(Reply("x", Usage(40, 2, cost_usd=0.2)))}, {"t": ["s/m"]})
    done = run(router.chat("t", MSG))
    assert done.usage.estimated is False and (done.usage.input_tokens, done.attempts) == (40, 1)


def test_failed_attempts_are_counted_as_wasted_tokens():
    blocked = Scripted(error=BackendError("blocked", Usage(9000, 1)))
    good = Scripted(Reply("ok", Usage(30, 2)))
    router = Router({"a": blocked, "b": good}, {"t": ["a/m", "b/m"]})
    done = run(router.chat("t", MSG))
    assert done.destination == "b/m" and done.attempts == 2
    assert done.wasted.total == 9001 and done.usage.total == 32


def test_unavailable_backends_are_not_attempts():
    router = Router(
        {"a": Scripted(error=Unavailable("no key")), "b": Scripted(Reply("ok", Usage(1, 1)))},
        {"t": ["a/m", "b/m"]},
    )
    done = run(router.chat("t", MSG))
    assert done.attempts == 1 and done.wasted.total == 0


def test_when_everything_fails_the_waste_travels_with_the_error():
    router = Router(
        {
            "a": Scripted(error=BackendError("down", Usage(500, 0))),
            "b": Scripted(error=BackendError("x")),
        },
        {"t": ["a/m", "b/m"]},
    )
    with pytest.raises(AllFailed) as exc:
        run(router.chat("t", MSG))
    assert exc.value.wasted.total == 500 and exc.value.attempts == 2
    assert isinstance(exc.value, BackendError)  # still catchable as before


# ---------------------------------------------------------------- the task


def test_task_records_reported_usage_and_cost(tmp_path):
    backend = Scripted(Reply("answer", Usage(7505, 4, 7503, 0.0015)))
    relay, _ = make_relay(tmp_path, backend=backend)
    task = run(relay.delegate("hello"))
    assert task["input_tokens"] == 7505 and task["output_tokens"] == 4
    assert task["cache_tokens"] == 7503 and task["cost_usd"] == 0.0015
    assert task["attempts"] == 1 and task["usage_estimated"] == 0 and task["wasted_tokens"] == 0
    assert "prompt" not in task


def test_task_marks_estimated_usage(tmp_path):
    relay, _ = make_relay(tmp_path, backend=Scripted(Reply("some answer")))
    task = run(relay.delegate("hello"))
    assert task["usage_estimated"] == 1 and task["input_tokens"] > 0
    assert "cost_usd" not in task  # unknown, not zero


def test_a_failed_task_keeps_the_tokens_its_attempts_wasted(tmp_path):
    relay, _ = make_relay(tmp_path, backend=Scripted(Reply("ok", Usage(10, 1))))
    ok = run(relay.delegate("hello"))
    assert ok["wasted_tokens"] == 0  # nothing failed here
    (tmp_path / "second").mkdir()
    failing = Scripted(error=BackendError("blocked", Usage(9000, 1)))
    relay2, _ = make_relay(tmp_path / "second", backend=failing)
    failed = run(relay2.delegate("hello"))
    assert failed["status"] == "failed" and failed["wasted_tokens"] == 9001
    assert failed["attempts"] == 1


# ------------------------------------------------------------------- storage


def test_old_database_gains_typed_usage_columns(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, kind TEXT, tier TEXT, status TEXT, "
        "description TEXT, model TEXT, result TEXT, error TEXT, saved_to TEXT, "
        "created TEXT, finished TEXT)"
    )
    db.execute(
        "INSERT INTO tasks (id, kind, tier, status, created) "
        "VALUES ('old1', 'text', 'light', 'completed', '2020-01-01T00:00:00')"
    )
    db.commit()
    db.close()
    store = TaskStore(path)
    types = {r["name"]: r["type"] for r in store.db.execute("PRAGMA table_info(tasks)")}
    assert types["input_tokens"] == "INTEGER" and types["cost_usd"] == "REAL"
    store.update("old1", input_tokens=5, cost_usd=0.0015)
    assert store.get("old1")["cost_usd"] == 0.0015 and store.get("old1")["input_tokens"] == 5


# -------------------------------------------------------------------- viewer


def seed(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    now = datetime.now().replace(microsecond=0)

    def add(task_id, tier, model, agent=None, days_ago=0, **fields):
        store.insert(task_id, "text", tier, "d", agent=agent)
        store.update(
            task_id,
            status=fields.pop("status", "completed"),
            model=model,
            created=(now - timedelta(days=days_ago)).isoformat(timespec="seconds"),
            **fields,
        )

    add("a1", "light", "agy/g", input_tokens=10_000, output_tokens=10, cost_usd=None)
    add("a2", "light", "agy/g", input_tokens=9_000, output_tokens=20, usage_estimated=1)
    add("b1", "heavy", "claude/s", agent="reviewer", input_tokens=7_505, output_tokens=4,
        cost_usd=0.0015)  # fmt: skip
    add("c1", "light", None, status="failed", wasted_tokens=9_001)
    add("old", "light", "agy/g", days_ago=30, input_tokens=1_000, output_tokens=1)
    add("legacy", "light", "agy/g")  # a row from before accounting: no numbers at all
    store.insert("t1", "transcription", "-", "audio")
    store.update("t1", status="completed", input_tokens=999)  # not a text task
    return store


def test_usage_rows_group_and_filter(tmp_path):
    store = seed(tmp_path)
    week = viewer.parse_since("7d")
    by_tier = {r["key"]: r for r in viewer.usage_rows(store.db, week, "tier")}
    assert set(by_tier) == {"light", "heavy"}
    assert by_tier["light"]["tasks"] == 3 and by_tier["light"]["input_tokens"] == 19_000
    assert by_tier["light"]["wasted_tokens"] == 9_001 and by_tier["light"]["estimated_tasks"] == 1
    assert by_tier["heavy"]["cost_usd"] == 0.0015 and by_tier["light"]["cost_usd"] is None
    assert (
        viewer.usage_rows(store.db, None, "tier")[1]["input_tokens"] == 20_000
    )  # 'light', all time
    by_model = {r["key"] for r in viewer.usage_rows(store.db, week, "model")}
    assert by_model == {"agy/g", "claude/s", "-"}
    by_agent = {r["key"] for r in viewer.usage_rows(store.db, week, "agent")}
    assert by_agent == {"reviewer", "-"}
    assert len(viewer.usage_rows(store.db, None, "day")) == 2  # today and 30 days ago
    with pytest.raises(ValueError, match="by must be"):
        viewer.usage_rows(store.db, None, "colour")


def test_parse_since():
    base = datetime(2026, 11, 16, 12, 0, 0)
    assert viewer.parse_since("24h", base) == base - timedelta(hours=24)
    assert viewer.parse_since("2w", base) == base - timedelta(weeks=2)
    assert viewer.parse_since("ALL") is None
    for bad in ("soon", "7", "d7", "-1d", ""):
        with pytest.raises(ValueError, match="not a period"):
            viewer.parse_since(bad)


def test_usage_report_text(tmp_path):
    seed(tmp_path)
    out = io.StringIO()
    viewer.show_usage(tmp_path / "t.db", "7d", "tier", out, PLAIN)
    text = out.getvalue()
    assert "Usage by tier (last 7d)" in text and "light" in text and "heavy" in text
    assert "19,000" in text and "$0.0015" in text and "TOTAL" in text
    assert "not reported" in text and "estimated" in text
    empty = io.StringIO()
    viewer.show_usage(tmp_path / "t.db", "1h", "tier", empty, PLAIN)
    assert "No usage recorded for the last 1h" in empty.getvalue() or "light" in empty.getvalue()


def test_usage_table_keeps_long_model_names_whole():
    row = {"key": "ollama/qwen2.5:7b-instruct-q4_K_M", "tasks": 1, "input_tokens": 34,
           "output_tokens": 2, "cache_tokens": 0, "wasted_tokens": 0, "cost_usd": None,
           "estimated_tasks": 0}  # fmt: skip
    assert "ollama/qwen2.5:7b-instruct-q4_K_M" in viewer.render_usage([row], "model", "7d", PLAIN)


def test_task_lines_and_detail_show_consumption():
    task = {"id": "abc", "status": "completed", "tier": "light", "created": "2026-11-16T10:00:00",
            "input_tokens": 7505, "output_tokens": 4, "cache_tokens": 7503, "cost_usd": 0.0015,
            "attempts": 2, "wasted_tokens": 9001, "usage_estimated": 0, "description": "d",
            "result": "r"}  # fmt: skip
    assert viewer.tokens_text(task) == "7.5k>4"
    assert viewer.tokens_text({**task, "usage_estimated": 1}) == "~7.5k>4"
    assert viewer.tokens_text({"id": "x"}) == "-"
    detail = viewer.usage_line(task)
    assert "in 7,505 (7,503 cached)" in detail and "wasted 9,001" in detail
    assert "$0.0015" in detail and "2 attempts" in detail
    assert viewer.usage_line({"id": "x"}) == ""
    assert "7.5k>4" in viewer.one_line(task, PLAIN)
    assert "Usage:" in viewer.render_full(task, PLAIN)


def test_cli_usage_command(tmp_path, capsys):
    seed(tmp_path)
    db = str(tmp_path / "t.db")
    assert main(["usage", "--db", db, "--since", "all", "--by", "model"]) == 0
    assert "Usage by model (all time)" in capsys.readouterr().out
    assert main(["usage", "--db", db, "--since", "soon"]) == 1
    assert "not a period" in capsys.readouterr().err


# ------------------------------------------------------------------ MCP tool


def test_get_usage_tool_returns_rows_total_and_notes(tmp_path):
    relay, _ = make_relay(tmp_path, backend=Scripted(Reply("x", Usage(100, 5, cost_usd=0.01))))
    run(relay.delegate("one"))
    run(relay.delegate("two"))
    report = relay.usage("24h", "tier")
    assert report["rows"][0]["key"] == "light" and report["rows"][0]["tasks"] == 2
    assert report["total"]["input_tokens"] == 200 and report["total"]["cost_usd"] == 0.02
    assert report["notes"]
    with pytest.raises(ValueError, match="not a period"):
        relay.usage("whenever")
    with pytest.raises(ValueError, match="by must be"):
        relay.usage("24h", "colour")


def test_config_validates_cli_output_mode():
    base = {"tiers": {"light": ["a/m"]}}
    ok = {"a": {"type": "cli", "command": "x", "output": "json"}}
    assert parse_config({**base, "backends": ok}).backends["a"]["output"] == "json"
    bad = {"a": {"type": "cli", "command": "x", "output": "xml"}}
    with pytest.raises(ConfigError, match="output must be"):
        parse_config({**base, "backends": bad})


def test_json_module_is_not_shadowed():  # guards the `json` import used by the CLI parser
    assert json.loads('{"a": 1}') == {"a": 1}
