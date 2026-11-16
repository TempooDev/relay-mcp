"""Project scope: .agents/ agents and skills, trust, `include`, and `relay-mcp init-project`."""

import asyncio
import os

import pytest

from relay_mcp import projects
from relay_mcp.cli import main
from relay_mcp.security import PathNotAllowed
from relay_mcp.server import Relay
from test_server import Recorder, make_relay, write_agent


def run(coro):
    return asyncio.run(coro)


def make_repo(tmp_path, name="repo"):
    """A repository inside the allowed folder used by make_relay."""
    root = tmp_path / "work" / name
    (root / ".git").mkdir(parents=True)
    return root


def project_agent(root, name, text):
    folder = root / ".agents" / "relay" / "agents"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(text, encoding="utf-8")


def skill(folder, name, text):
    (folder / name).mkdir(parents=True, exist_ok=True)
    (folder / name / "SKILL.md").write_text(text, encoding="utf-8")


# ------------------------------------------------------------- root and trust


def test_find_project_root(tmp_path):
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    (repo / "src" / "deep").mkdir(parents=True)
    assert projects.find_project_root(repo / "src" / "deep") == repo
    other = tmp_path / "o"
    (other / ".agents").mkdir(parents=True)
    assert projects.find_project_root(other) == other
    plain = tmp_path / "plain"
    plain.mkdir()
    assert projects.find_project_root(plain) == plain  # nothing to climb to


def test_trusting_a_folder_trusts_everything_below(tmp_path):
    assert projects.is_trusted(tmp_path / "a" / "b", [tmp_path / "a"])
    assert projects.is_trusted(tmp_path / "a", [tmp_path / "a"])
    assert not projects.is_trusted(tmp_path / "ab", [tmp_path / "a"])


def test_untrusted_project_is_not_loaded(tmp_path):
    relay, _ = make_relay(tmp_path)
    repo = make_repo(tmp_path)
    project_agent(repo, "pa", "---\n---\nproject prompt")
    with pytest.raises(ValueError, match="not trusted"):
        run(relay.run_agent("pa", "x", project=str(repo)))
    with pytest.raises(ValueError, match="not trusted"):
        relay.list_agents(str(repo))


def test_trusted_project_agent_runs_with_its_own_model(tmp_path):
    rec = Recorder()
    repo = make_repo(tmp_path)
    relay, _ = make_relay(tmp_path, backend=rec, trusted=[repo])
    project_agent(repo, "pa", "---\ndescription: mine\nmodel: s/special\n---\nproject prompt")
    task = run(relay.run_agent("pa", "do it", project=str(repo)))
    model, messages = rec.calls[0]
    assert model == "special" and messages[0]["content"] == "project prompt"
    listed = {a["name"]: a for a in relay.list_agents(str(repo))["agents"]}
    assert listed["pa"]["scope"] == "project" and task["agent"] == "pa"


def test_project_agent_overrides_global_agent_of_the_same_name(tmp_path):
    rec = Recorder()
    repo = make_repo(tmp_path)
    relay, _ = make_relay(tmp_path, backend=rec, trusted=[repo])
    write_agent(tmp_path, "twin", "---\n---\nglobal prompt")
    project_agent(repo, "twin", "---\n---\nproject prompt")
    run(relay.run_agent("twin", "x"))  # no project: the global one
    run(relay.run_agent("twin", "x", project=str(repo)))
    assert [c[1][0]["content"] for c in rec.calls] == ["global prompt", "project prompt"]


def test_subfolder_resolves_to_the_repository_root(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "src").mkdir()
    relay, _ = make_relay(tmp_path, trusted=[repo])
    assert relay.list_agents(str(repo / "src"))["project"] == str(repo.resolve())


def test_default_project_is_used_and_trusted_without_configuration(tmp_path):
    rec = Recorder()
    repo = make_repo(tmp_path)
    project_agent(repo, "pa", "---\n---\nproject prompt")
    relay, _ = make_relay(tmp_path, backend=rec, project=repo)  # like `relay-mcp --project`
    run(relay.run_agent("pa", "x"))
    assert rec.calls[0][1][0]["content"] == "project prompt"


def test_default_project_outside_the_allowed_folders_is_refused(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(PathNotAllowed):
        make_relay(tmp_path, project=outside)


def test_broken_project_agent_is_reported_with_a_project_prefix(tmp_path):
    repo = make_repo(tmp_path)
    relay, _ = make_relay(tmp_path, trusted=[repo])
    project_agent(repo, "bad", "---\ncolour: red\n---\nx")
    errors = relay.list_agents(str(repo))["errors"]
    assert "colour" in errors["project:bad.md"]


# -------------------------------------------------------------------- include


def populate(repo):
    (repo / "src" / "sub").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("A_CODE", encoding="utf-8")
    (repo / "src" / "sub" / "b.py").write_text("B_CODE", encoding="utf-8")
    (repo / "README.md").write_text("README_TEXT", encoding="utf-8")
    (repo / ".env").write_text("SECRET=1", encoding="utf-8")
    (repo / "src" / ".env.local").write_text("SECRET=2", encoding="utf-8")
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "node_modules" / "pkg" / "x.py").write_text("DEPENDENCY", encoding="utf-8")
    (repo / "src" / "blob.py").write_bytes(b"\x00\x01binary")


def sent_to_model(rec):
    return rec.calls[-1][1][-1]["content"]


def test_include_attaches_matching_files_by_relative_path(tmp_path):
    rec = Recorder()
    repo = make_repo(tmp_path)
    populate(repo)
    relay, _ = make_relay(tmp_path, backend=rec, trusted=[repo])
    project_agent(repo, "ex", "---\ninclude: src/**/*.py, README.md\n---\nexplore")
    run(relay.run_agent("ex", "map it", project=str(repo)))
    sent = sent_to_model(rec)
    assert '<file path="src/a.py">' in sent and "A_CODE" in sent
    assert '<file path="src/sub/b.py">' in sent and "README_TEXT" in sent
    assert "DEPENDENCY" not in sent  # node_modules is never descended into
    assert "binary" not in sent  # binary files are skipped
    assert sent.endswith("map it")
    assert str(repo) not in sent  # only relative paths are sent to the model


def test_include_never_pulls_in_secrets_even_with_a_broad_pattern(tmp_path):
    rec = Recorder()
    repo = make_repo(tmp_path)
    populate(repo)
    relay, _ = make_relay(tmp_path, backend=rec, trusted=[repo])
    project_agent(repo, "all", "---\ninclude: **/*\n---\nexplore")
    run(relay.run_agent("all", "x", project=str(repo)))
    sent = sent_to_model(rec)
    assert "SECRET" not in sent and "A_CODE" in sent


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks needs special rights on Windows")
def test_include_skips_symlinks_that_leave_the_allowed_folders(tmp_path):
    rec = Recorder()
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE_SECRET")
    (repo / "link.txt").symlink_to(outside)
    (repo / "ok.txt").write_text("INSIDE")
    relay, _ = make_relay(tmp_path, backend=rec, trusted=[repo])
    project_agent(repo, "ex", "---\ninclude: *.txt\n---\nexplore")
    run(relay.run_agent("ex", "x", project=str(repo)))
    assert "OUTSIDE_SECRET" not in sent_to_model(rec) and "INSIDE" in sent_to_model(rec)


def test_include_without_a_project_is_an_error(tmp_path):
    relay, _ = make_relay(tmp_path)
    write_agent(tmp_path, "needs", "---\ninclude: src/*.py\n---\nexplore")
    with pytest.raises(ValueError, match="needs a project"):
        run(relay.run_agent("needs", "x"))


def test_include_limits_are_enforced(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    populate(repo)
    relay, _ = make_relay(tmp_path, trusted=[repo])
    project_agent(repo, "ex", "---\ninclude: src/**/*.py\n---\nexplore")
    monkeypatch.setattr(projects, "MAX_INCLUDE_FILES", 1)
    with pytest.raises(ValueError, match="more than 1 files"):
        run(relay.run_agent("ex", "x", project=str(repo)))
    monkeypatch.setattr(projects, "MAX_INCLUDE_FILES", 200)
    relay.config.server.max_attachment_chars = 8
    with pytest.raises(ValueError, match="exceed the limit"):
        run(relay.run_agent("ex", "x", project=str(repo)))


# --------------------------------------------------------------------- skills


def test_agent_gets_the_skills_it_asks_for(tmp_path):
    rec = Recorder()
    repo = make_repo(tmp_path)
    relay, _ = make_relay(tmp_path, backend=rec, trusted=[repo])
    skill(
        repo / ".agents" / "skills",
        "security",
        "---\nname: security\ndescription: s\n---\nSKILL BODY",
    )
    project_agent(repo, "rev", "---\nskills: security\n---\nBase prompt.")
    run(relay.run_agent("rev", "x", project=str(repo)))
    system = rec.calls[0][1][0]["content"]
    assert system.startswith("Base prompt.") and "# Skill: security\nSKILL BODY" in system


def test_skills_are_read_from_both_agents_and_claude_folders(tmp_path):
    repo = make_repo(tmp_path)
    relay, _ = make_relay(tmp_path, trusted=[repo])
    skill(repo / ".agents" / "skills", "one", "---\ndescription: from agents\n---\nbody one")
    skill(repo / ".claude" / "skills", "two", "---\ndescription: from claude\n---\nbody two")
    skill(repo / ".claude" / "skills", "one", "---\ndescription: duplicate\n---\nother body")
    listed = {s["name"]: s for s in relay.list_skills(str(repo))["skills"]}
    assert set(listed) == {"one", "two"}
    assert listed["one"]["description"] == "from agents"  # .agents wins a clash, no error
    assert relay.list_skills(str(repo))["errors"] == {}


def test_skills_written_for_other_tools_load_with_extra_front_matter(tmp_path):
    repo = make_repo(tmp_path)
    relay, _ = make_relay(tmp_path, trusted=[repo])
    text = (
        "---\nname: pdf\ndescription: Work with PDFs\n"
        "allowed-tools:\n  - Read\n  - Bash\n---\nUse it."
    )
    skill(repo / ".claude" / "skills", "pdf", text)
    skill(repo / ".agents" / "skills", "bare", "Just instructions, no front matter.")
    names = {s["name"] for s in relay.list_skills(str(repo))["skills"]}
    assert names == {"pdf", "bare"}


def test_global_skills_folder_and_project_override(tmp_path):
    rec = Recorder()
    repo = make_repo(tmp_path)
    relay, _ = make_relay(tmp_path, backend=rec, trusted=[repo])
    skill(tmp_path / "skills", "style", "---\n---\nGLOBAL STYLE")
    skill(repo / ".agents" / "skills", "style", "---\n---\nPROJECT STYLE")
    write_agent(tmp_path, "writer", "---\nskills: style\n---\nWrite.")
    run(relay.run_agent("writer", "x"))
    run(relay.run_agent("writer", "x", project=str(repo)))
    assert "GLOBAL STYLE" in rec.calls[0][1][0]["content"]
    assert "PROJECT STYLE" in rec.calls[1][1][0]["content"]


def test_missing_skill_is_a_clear_error(tmp_path):
    relay, _ = make_relay(tmp_path)
    skill(tmp_path / "skills", "real", "---\n---\nbody")
    write_agent(tmp_path, "needy", "---\nskills: ghost\n---\nbody")
    with pytest.raises(ValueError, match=r"ghost.*Available: real"):
        run(relay.run_agent("needy", "x"))


# ------------------------------------------------------------------ init-project


def test_init_project_creates_starter_agents_and_never_overwrites(tmp_path, capsys):
    assert main(["init-project", str(tmp_path)]) == 0
    folder = tmp_path / ".agents" / "relay" / "agents"
    assert {p.name for p in folder.glob("*.md")} >= {"explorer.md", "summarizer.md"}
    out = capsys.readouterr().out
    assert "--project" in out and "trusted" in out
    (folder / "explorer.md").write_text("MY EDIT", encoding="utf-8")
    assert main(["init-project", str(tmp_path)]) == 0
    assert (folder / "explorer.md").read_text(encoding="utf-8") == "MY EDIT"
    assert "kept" in capsys.readouterr().out


def test_init_project_rejects_a_missing_folder(tmp_path, capsys):
    assert main(["init-project", str(tmp_path / "nope")]) == 1
    assert "not a folder" in capsys.readouterr().err


def test_starter_agents_parse_with_the_example_config():
    import tomllib
    from importlib import resources

    from relay_mcp.agents import load_agents
    from relay_mcp.config import parse_config

    cfg = parse_config(
        tomllib.loads(
            resources.files("relay_mcp").joinpath("relay.example.toml").read_text(encoding="utf-8")
        )
    )
    folder = resources.files("relay_mcp").joinpath("templates", "agents")
    with resources.as_file(folder) as path:
        agents, errors = load_agents(path, list(cfg.tiers), set(cfg.backends))
    assert errors == {} and {"explorer", "researcher", "reviewer", "summarizer"} <= set(agents)


def test_relay_class_accepts_a_default_project(tmp_path):
    repo = make_repo(tmp_path)
    relay, _ = make_relay(tmp_path, project=repo)
    assert isinstance(relay, Relay) and relay.default_project == repo.resolve()
