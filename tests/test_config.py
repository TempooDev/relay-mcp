import tomllib
from importlib import resources

import pytest

from relay_mcp.config import ConfigError, find_config, load_config, parse_config

BASE = {
    "backends": {"a": {"base_url": "http://x/v1"}},
    "tiers": {"light": ["a/model"]},
}


def example_data():
    text = resources.files("relay_mcp").joinpath("relay.example.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)


def test_packaged_example_is_valid():
    cfg = parse_config(example_data())
    assert {"light", "local", "heavy", "max"} <= set(cfg.tiers)


def test_defaults():
    cfg = parse_config(BASE)
    assert cfg.server.host == "127.0.0.1"
    assert cfg.backends["a"]["type"] == "openai"
    assert cfg.security.allowed_roots  # defaults to the home folder


def test_unknown_backend_in_tier():
    with pytest.raises(ConfigError, match="unknown backend"):
        parse_config({**BASE, "tiers": {"light": ["nope/model"]}})


def test_destination_format():
    with pytest.raises(ConfigError, match="backend/model"):
        parse_config({**BASE, "tiers": {"light": ["justamodel"]}})


def test_cli_backend_requires_command():
    with pytest.raises(ConfigError, match="command"):
        parse_config({**BASE, "backends": {"a": {"type": "cli"}}})


def test_bad_error_pattern_regex_is_rejected():
    cli = {"a": {"type": "cli", "command": "x", "error_patterns": ["(unclosed"]}}
    with pytest.raises(ConfigError, match="bad regex"):
        parse_config({**BASE, "backends": cli})


def test_transcription_must_use_openai_backend():
    cli = {"a": {"type": "cli", "command": "x"}}
    with pytest.raises(ConfigError, match="transcription"):
        parse_config({**BASE, "backends": cli, "transcription": {"backend": "a", "model": "m"}})


def test_relative_roots_rejected():
    with pytest.raises(ConfigError, match="absolute"):
        parse_config({**BASE, "security": {"allowed_roots": ["relative/dir"]}})


def test_env_var_and_cwd_lookup(tmp_path, monkeypatch):
    cfg_file = tmp_path / "custom.toml"
    cfg_file.write_text('[backends.a]\nbase_url="http://x/v1"\n[tiers]\nlight=["a/m"]\n')
    monkeypatch.setenv("RELAY_MCP_CONFIG", str(cfg_file))
    assert find_config() == cfg_file
    assert load_config().source == cfg_file


def test_missing_config_message(tmp_path, monkeypatch):
    monkeypatch.delenv("RELAY_MCP_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    with pytest.raises(ConfigError, match="relay-mcp init"):
        find_config()


def test_output_extensions_validation_and_default():
    assert ".md" in parse_config(BASE).security.output_extensions
    cfg = parse_config({**BASE, "security": {"output_extensions": [".PY"]}})
    assert cfg.security.output_extensions == [".py"]
    with pytest.raises(ConfigError, match="output_extensions"):
        parse_config({**BASE, "security": {"output_extensions": ["md"]}})


def test_agents_dir_defaults_next_to_config_and_can_be_moved(tmp_path):
    src = tmp_path / "relay.toml"
    assert parse_config(BASE, source=src).agents_dir == tmp_path / "agents"
    custom = parse_config({**BASE, "agents": {"dir": "team/agents"}}, source=src)
    assert custom.agents_dir == tmp_path / "team" / "agents"
    absolute = parse_config({**BASE, "agents": {"dir": str(tmp_path / "x")}}, source=src)
    assert absolute.agents_dir == tmp_path / "x"


def test_trusted_projects_and_skills_dir(tmp_path):
    src = tmp_path / "relay.toml"
    cfg = parse_config(BASE, source=src)
    assert cfg.trusted_projects == [] and cfg.skills_dir == tmp_path / "skills"
    trusted = str(tmp_path / "repo")
    cfg = parse_config(
        {**BASE, "projects": {"trusted": [trusted]}, "skills": {"dir": "my/skills"}}, source=src
    )
    assert cfg.trusted_projects == [tmp_path / "repo"]
    assert cfg.skills_dir == tmp_path / "my" / "skills"
    with pytest.raises(ConfigError, match="absolute"):
        parse_config({**BASE, "projects": {"trusted": ["relative/repo"]}})


def test_paths_stay_absolute_when_the_config_is_given_as_a_relative_path(tmp_path, monkeypatch):
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "relay.toml").write_text(
        '[server]\ndb = "tasks.db"\n[backends.a]\nbase_url="http://x/v1"\n[tiers]\nlight=["a/m"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path / "cfg")
    cfg = load_config("relay.toml")  # relative, as typed with `-c relay.toml`
    monkeypatch.chdir(tmp_path)  # the working directory changes afterwards
    expected = tmp_path / "cfg"
    assert cfg.agents_dir == expected / "agents" and cfg.agents_dir.is_absolute()
    assert cfg.skills_dir == expected / "skills" and cfg.skills_dir.is_absolute()
    assert cfg.server.db == expected / "tasks.db" and cfg.server.db.is_absolute()


def test_store_prompts_defaults_to_off():
    assert parse_config(BASE).server.store_prompts is False
    assert parse_config({**BASE, "server": {"store_prompts": True}}).server.store_prompts is True


def test_relative_db_is_next_to_config(tmp_path):
    cfg = parse_config({**BASE, "server": {"db": "t.db"}}, source=tmp_path / "relay.toml")
    assert cfg.server.db == tmp_path / "t.db"
