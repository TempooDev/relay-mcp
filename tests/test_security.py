import os

import pytest

from relay_mcp.config import DEFAULT_DENY_PATTERNS
from relay_mcp.security import PathNotAllowed, PathPolicy


@pytest.fixture
def policy(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    return PathPolicy([root], DEFAULT_DENY_PATTERNS), root


def test_inside_root_allowed(policy):
    pol, root = policy
    (root / "a.txt").write_text("x")
    assert pol.check(str(root / "a.txt")) == (root / "a.txt").resolve()


def test_outside_root_rejected(policy, tmp_path):
    pol, _ = policy
    with pytest.raises(PathNotAllowed, match="outside"):
        pol.check(str(tmp_path / "other.txt"))


def test_traversal_rejected(policy):
    pol, root = policy
    with pytest.raises(PathNotAllowed, match="outside"):
        pol.check(str(root / ".." / "secret.txt"))


def test_relative_rejected(policy):
    pol, _ = policy
    with pytest.raises(PathNotAllowed, match="absolute"):
        pol.check("notes.txt")


@pytest.mark.parametrize("name", [".env", ".env.local", "server.pem", "id_rsa", "credentials.json"])
def test_protected_names_rejected(policy, name):
    pol, root = policy
    with pytest.raises(PathNotAllowed, match="protected"):
        pol.check(str(root / name))


def test_protected_folder_rejected(policy):
    pol, root = policy
    with pytest.raises(PathNotAllowed, match="protected"):
        pol.check(str(root / ".ssh" / "config"))


def test_output_never_overwrites(policy):
    pol, root = policy
    (root / "exists.md").write_text("x")
    with pytest.raises(PathNotAllowed, match="already exists"):
        pol.check_output(str(root / "exists.md"))
    assert pol.check_output(str(root / "new.md")) == (root / "new.md").resolve()


@pytest.mark.parametrize(
    "name", ["run.bat", "x.ps1", "tool.EXE", "page.hta", "a.reg", "s.sh", "noext", "m.py"]
)
def test_output_only_allows_listed_extensions(policy, name):
    pol, root = policy
    with pytest.raises(PathNotAllowed, match="extension"):
        pol.check_output(str(root / name))


@pytest.mark.parametrize("name", ["notes.md", "OUT.TXT", "data.json", "subs.srt"])
def test_output_allows_text_formats(policy, name):
    pol, root = policy
    assert pol.check_output(str(root / name)).name == name


def test_output_extensions_are_configurable(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    pol = PathPolicy([root], DEFAULT_DENY_PATTERNS, output_extensions=[".py"])
    assert pol.check_output(str(root / "a.py")).name == "a.py"
    with pytest.raises(PathNotAllowed):
        pol.check_output(str(root / "a.md"))


# Regression: on Windows these names create real run.bat / run.ps1 files, and used to slip
# past the old "dangerous extension" deny-list.
@pytest.mark.parametrize("name", ["y.bat.", "z.bat ", "w.bat::$DATA", "u.ps1.", "ok.md.", "ok.md "])
def test_output_rejects_windows_name_tricks(policy, name):
    pol, root = policy
    with pytest.raises(PathNotAllowed):
        pol.check_output(str(root / name))


@pytest.mark.skipif(os.name != "nt", reason="NTFS alternate data streams are Windows-only")
@pytest.mark.parametrize("name", [".env:stream", "notes.txt:hidden", "evil.bat:.md"])
def test_colon_in_names_rejected_on_windows(policy, name):
    pol, root = policy
    with pytest.raises(PathNotAllowed, match="':'"):
        pol.check(str(root / name))


@pytest.mark.skipif(os.name != "nt", reason="Windows normalises these names")
@pytest.mark.parametrize("name", [".env::$DATA", ".env.", ".env ", ".ENV"])
def test_deny_list_survives_windows_name_tricks(policy, name):
    pol, root = policy
    (root / ".env").write_text("SECRET=1")
    with pytest.raises(PathNotAllowed):
        pol.check(str(root / name))
