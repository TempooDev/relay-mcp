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


@pytest.mark.parametrize("name", ["run.bat", "x.ps1", "tool.EXE"])
def test_output_refuses_executables(policy, name):
    pol, root = policy
    with pytest.raises(PathNotAllowed, match="executable"):
        pol.check_output(str(root / name))
