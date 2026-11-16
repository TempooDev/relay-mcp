import asyncio
import sys

import httpx
import pytest

from relay_mcp.backends import BackendError, CliBackend, OpenAIBackend, Router, Unavailable

MSG = [{"role": "user", "content": "hello"}]


def cli(args, **extra):
    return CliBackend("t", {"type": "cli", "command": sys.executable, "args": args, **extra})


def openai(handler, **cfg):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAIBackend("o", {"base_url": "http://x/v1", **cfg}, client=client)


def test_cli_reads_prompt_from_stdin():
    b = cli(["-c", "import sys;print(sys.stdin.read().upper())"])
    assert asyncio.run(b.chat("m", MSG)) == "HELLO"


def test_cli_prompt_argument_and_model_placeholder():
    b = cli(["-c", "import sys;print(sys.argv[1], sys.argv[2])", "{model}"], prompt_arg="--p")
    assert asyncio.run(b.chat("modelX", MSG)) == "modelX --p=hello"


def test_cli_argument_limit():
    b = cli(["-c", "pass"], prompt_arg="--p", max_arg_chars=3)
    with pytest.raises(BackendError, match="at most 3"):
        asyncio.run(b.chat("m", MSG))


def test_cli_nonzero_exit():
    with pytest.raises(BackendError, match="exit code 3"):
        asyncio.run(cli(["-c", "import sys;sys.exit(3)"]).chat("m", MSG))


def test_cli_error_pattern_turns_a_fake_answer_into_a_failure():
    script = "print('This request was BLOCKED by the filters.')"
    b = cli(["-c", script], error_patterns=["blocked by the filters"])  # case-insensitive
    with pytest.raises(BackendError, match="reported an error"):
        asyncio.run(b.chat("m", MSG))
    plain = cli(["-c", script])  # without the pattern it would be accepted as an answer
    assert "BLOCKED" in asyncio.run(plain.chat("m", MSG))


def test_error_pattern_makes_the_router_try_the_next_destination():
    blocked = cli(["-c", "print('blocked by filters')"], error_patterns=["blocked"])
    ok = openai(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}))
    router = Router({"c": blocked, "o": ok}, {"t": ["c/x", "o/y"]})
    assert asyncio.run(router.chat("t", MSG)) == ("ok", "o/y")


def test_cli_not_installed():
    b = CliBackend("t", {"type": "cli", "command": "definitely-not-a-real-binary-xyz"})
    with pytest.raises(Unavailable):
        asyncio.run(b.chat("m", MSG))


def test_cli_static_model_list():
    b = cli([], models=["a", "b"])
    assert asyncio.run(b.list_models())["models"] == ["a", "b"]


def test_openai_chat_and_auth_header(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "secret")
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.read()
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    b = openai(handler, api_key_env="TEST_KEY")
    assert asyncio.run(b.chat("m", MSG)) == "hi"
    assert seen["auth"] == "Bearer secret"
    assert b"hello" in seen["body"]


def test_openai_missing_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("TEST_KEY", raising=False)
    b = openai(lambda r: httpx.Response(200), api_key_env="TEST_KEY")
    with pytest.raises(Unavailable, match="TEST_KEY"):
        asyncio.run(b.chat("m", MSG))
    assert asyncio.run(b.list_models())["status"].startswith("skipped")


def test_openai_http_error():
    b = openai(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(BackendError, match="HTTP 500"):
        asyncio.run(b.chat("m", MSG))


def test_openai_list_models():
    b = openai(lambda r: httpx.Response(200, json={"data": [{"id": "z"}, {"id": "a"}]}))
    assert asyncio.run(b.list_models())["models"] == ["a", "z"]


def test_flatten_is_plain_for_one_turn_and_labelled_with_history():
    from relay_mcp.backends import flatten

    assert (
        flatten([{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]) == "S\n\nU"
    )
    history = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    assert flatten(history) == "S\n\nUser: q1\n\nAssistant: a1\n\nUser: q2"


def test_router_chat_to_uses_explicit_destinations_in_order():
    broken = openai(lambda r: httpx.Response(500, text="down"))
    ok = openai(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}))
    router = Router({"b": broken, "o": ok}, {"t": ["b/x"]})
    assert asyncio.run(router.chat_to(["b/x", "o/y"], MSG)) == ("ok", "o/y")


def test_router_falls_back_in_order(monkeypatch):
    monkeypatch.delenv("NOKEY", raising=False)
    skipped = openai(lambda r: httpx.Response(200), api_key_env="NOKEY")
    broken = openai(lambda r: httpx.Response(500, text="down"))
    ok = openai(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}))
    router = Router(
        {"skipped": skipped, "broken": broken, "ok": ok},
        {"t": ["skipped/m", "broken/m", "ok/m2"]},
    )
    assert asyncio.run(router.chat("t", MSG)) == ("ok", "ok/m2")


def test_router_reports_every_failure():
    broken = openai(lambda r: httpx.Response(500, text="down"))
    router = Router({"b": broken}, {"t": ["b/one", "b/two"]})
    with pytest.raises(BackendError) as exc:
        asyncio.run(router.chat("t", MSG))
    assert "b/one" in str(exc.value) and "b/two" in str(exc.value)
