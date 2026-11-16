import pytest

from relay_mcp.agents import (
    AgentError,
    load_agents,
    load_skills,
    parse_agent,
    parse_skill,
    split_front_matter,
)

TIERS = ["light", "heavy"]
BACKENDS = {"claude", "agy"}


def parse(text, name="file"):
    return parse_agent(text, name, TIERS, BACKENDS)


def test_minimal_agent_defaults_to_the_light_tier():
    agent = parse("---\n---\nDo the thing.", "worker")
    assert (agent.name, agent.tier, agent.destinations) == ("worker", "light", None)
    assert agent.system == "Do the thing."


def test_full_agent_with_model_list_and_quotes():
    text = (
        "---\n"
        "name: Explorer\n"
        'description: "Maps code: where things live"\n'
        "model: claude/haiku, agy/gemini-3.8-flash-medium\n"
        "---\n"
        "You explore.\n\nSecond paragraph."
    )
    agent = parse(text)
    assert agent.name == "explorer"
    assert agent.description == "Maps code: where things live"
    assert agent.destinations == ("claude/haiku", "agy/gemini-3.8-flash-medium")
    assert agent.system == "You explore.\n\nSecond paragraph."
    assert agent.route == "claude/haiku, agy/gemini-3.8-flash-medium"


def test_tier_agent_route():
    assert parse("---\ntier: heavy\n---\nx").route == "tier:heavy"


def test_comments_blank_lines_and_bom_are_tolerated():
    text = "﻿---\n# a note\n\ndescription: ok\n---\nbody"
    assert parse(text).description == "ok"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("no front matter", "must start with"),
        ("---\nname: a\nbody", "not closed"),
        ("---\n---\n   \n", "body"),
        ("---\nmodel: claude/haiku\ntier: light\n---\nx", "either"),
        ("---\ntier: nope\n---\nx", "unknown tier"),
        ("---\nmodel: nobackend/x\n---\nx", "unknown backend"),
        ("---\nmodel: justamodel\n---\nx", "backend/model"),
        ("---\ncolour: red\n---\nx", "unknown key"),
        ("---\nnot a pair\n---\nx", "key: value"),
        ("---\nname: Bad Name!\n---\nx", "lowercase"),
    ],
)
def test_invalid_agents_are_rejected_with_a_reason(text, message):
    with pytest.raises(AgentError, match=message):
        parse(text)


def test_include_and_skills_are_parsed_as_lists():
    agent = parse("---\ninclude: src/**/*.py, README.md\nskills: Security, style\n---\nbody")
    assert agent.include == ("src/**/*.py", "README.md")
    assert agent.skills == ("security", "style")
    assert parse("---\n---\nbody").include == () and parse("---\n---\nbody").skills == ()


@pytest.mark.parametrize(
    "pattern", ["../x", "src/../../x", "/etc/passwd", "C:/Users/x", "~/x", "..\\x"]
)
def test_include_patterns_cannot_leave_the_project(pattern):
    with pytest.raises(AgentError, match="inside the project"):
        parse(f"---\ninclude: {pattern}\n---\nbody")


def test_bad_skill_reference_is_rejected():
    with pytest.raises(AgentError, match="skill name"):
        parse("---\nskills: Bad Name!\n---\nbody")


def test_skill_parsing_is_lenient_about_unknown_keys_and_missing_front_matter():
    skill = parse_skill("---\nname: pdf\nallowed-tools:\n  - Read\n---\nbody", "x", None, "project")
    assert (skill.name, skill.body, skill.scope) == ("pdf", "body", "project")
    assert parse_skill("only instructions", "folder-name", None, "global").name == "folder-name"
    with pytest.raises(AgentError, match="no instructions"):
        parse_skill("---\nname: empty\n---\n", "x", None, "global")


def test_load_skills_first_folder_wins_and_reports_errors(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    for folder, text in ((first, "from first"), (second, "from second")):
        (folder / "dup").mkdir(parents=True)
        (folder / "dup" / "SKILL.md").write_text(text, encoding="utf-8")
    (second / "broken").mkdir()
    (second / "broken" / "SKILL.md").write_text("---\nname: Bad Name!\n---\nx", encoding="utf-8")
    (second / "no-file").mkdir()  # a folder without SKILL.md is ignored
    skills, errors = load_skills([first, second, tmp_path / "missing"], "project")
    assert skills["dup"].body == "from first"
    assert "lowercase" in errors["broken/SKILL.md"] and len(errors) == 1


def test_split_front_matter_returns_body():
    meta, body = split_front_matter("---\ntier: light\n---\n\nhello\n")
    assert meta == {"tier": "light"} and body == "hello"


def test_load_agents_reads_markdown_files_only_and_reports_errors(tmp_path):
    (tmp_path / "ok.md").write_text("---\n---\nbody", encoding="utf-8")
    (tmp_path / "broken.md").write_text("nothing here", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    agents, errors = load_agents(tmp_path, TIERS, BACKENDS)
    assert list(agents) == ["ok"]
    assert "must start with" in errors["broken.md"]


def test_load_agents_rejects_duplicate_names(tmp_path):
    (tmp_path / "a.md").write_text("---\nname: same\n---\nx", encoding="utf-8")
    (tmp_path / "b.md").write_text("---\nname: same\n---\nx", encoding="utf-8")
    agents, errors = load_agents(tmp_path, TIERS, BACKENDS)
    assert list(agents) == ["same"] and "already used" in errors["b.md"]


def test_missing_directory_is_not_an_error(tmp_path):
    assert load_agents(tmp_path / "nope", TIERS, BACKENDS) == ({}, {})
