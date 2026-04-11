"""Test that safehouse sandbox allows git operations via local-overrides.sb.

Creates a temporary git repo, then runs basic non-destructive git commands
inside safehouse with the project's append-profile to verify the sandbox
policy grants the necessary filesystem and process access.
"""

import os
import subprocess
from pathlib import Path

import pytest

SAFEHOUSE = "safehouse"
LOCAL_OVERRIDES = Path(__file__).resolve().parent.parent / "safehouse" / "local-overrides.sb"
USER_PROFILE = os.environ.get("SAFEHOUSE_APPEND_PROFILE")


def _safehouse_available() -> bool:
    """Check that safehouse is installed AND sandbox-exec works (not nested)."""
    try:
        r = subprocess.run(
            [SAFEHOUSE, "--", "true"],
            capture_output=True, timeout=10,
        )
        return r.returncode == 0
    except FileNotFoundError:
        return False


pytestmark = pytest.mark.skipif(
    not _safehouse_available(),
    reason="safehouse not installed or sandbox-exec unavailable (nested sandbox?)",
)


def _run_in_safehouse(cmd: list[str], cwd: str) -> subprocess.CompletedProcess:
    """Run a command inside safehouse with project + user append-profiles."""
    full_cmd = [SAFEHOUSE, f"--append-profile={LOCAL_OVERRIDES}"]
    if USER_PROFILE:
        full_cmd.append(f"--append-profile={USER_PROFILE}")
    full_cmd += ["--", *cmd]
    return subprocess.run(
        full_cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=30,
    )


@pytest.fixture
def git_repo(tmp_path):
    """Create a minimal git repo outside safehouse (setup)."""
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(tmp_path), capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_path), capture_output=True,
    )
    (tmp_path / "hello.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=str(tmp_path), capture_output=True, check=True,
    )
    return tmp_path


class TestSafehouseGitAccess:
    """Verify that git commands work inside safehouse with local-overrides.sb."""

    def test_git_status(self, git_repo):
        result = _run_in_safehouse(["git", "status", "--porcelain"], cwd=str(git_repo))
        assert result.returncode == 0, f"git status failed: {result.stderr}"
        assert result.stdout.strip() == ""  # clean repo

    def test_git_log(self, git_repo):
        result = _run_in_safehouse(["git", "log", "--oneline"], cwd=str(git_repo))
        assert result.returncode == 0, f"git log failed: {result.stderr}"
        assert "initial commit" in result.stdout

    def test_git_branch(self, git_repo):
        result = _run_in_safehouse(["git", "branch", "--list"], cwd=str(git_repo))
        assert result.returncode == 0, f"git branch failed: {result.stderr}"
        assert "main" in result.stdout or "master" in result.stdout

    def test_git_diff_empty(self, git_repo):
        result = _run_in_safehouse(["git", "diff"], cwd=str(git_repo))
        assert result.returncode == 0, f"git diff failed: {result.stderr}"
        assert result.stdout.strip() == ""

    def test_git_diff_with_changes(self, git_repo):
        (git_repo / "hello.py").write_text("print('modified')\n")
        result = _run_in_safehouse(["git", "diff"], cwd=str(git_repo))
        assert result.returncode == 0, f"git diff failed: {result.stderr}"
        assert "modified" in result.stdout

    def test_git_show_head(self, git_repo):
        result = _run_in_safehouse(["git", "show", "--stat", "HEAD"], cwd=str(git_repo))
        assert result.returncode == 0, f"git show failed: {result.stderr}"
        assert "hello.py" in result.stdout

    def test_git_rev_parse(self, git_repo):
        result = _run_in_safehouse(["git", "rev-parse", "HEAD"], cwd=str(git_repo))
        assert result.returncode == 0, f"git rev-parse failed: {result.stderr}"
        # SHA-1 hex string
        assert len(result.stdout.strip()) == 40

    def test_gitconfig_readable(self, git_repo):
        """Verify the sandbox allows reading ~/.gitconfig (from local-overrides.sb)."""
        result = _run_in_safehouse(
            ["git", "config", "--global", "--list"],
            cwd=str(git_repo),
        )
        # returncode 0 means it could read the file; non-zero with "unable to access"
        # would indicate the sandbox blocked it. An empty global config is also fine.
        assert result.returncode == 0 or "unable to access" not in result.stderr, (
            f"Sandbox blocked reading ~/.gitconfig: {result.stderr}"
        )
