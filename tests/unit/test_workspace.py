"""Unit tests for workspace.py — git operations and scanning."""

import subprocess
import os
from pathlib import Path

import pytest
from auto import workspace


@pytest.fixture
def git_repo(tmp_path):
    """Create a minimal git repo."""
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(tmp_path), capture_output=True)
    (tmp_path / "main.py").write_text("print('hello')\n")
    (tmp_path / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path),
                   capture_output=True, check=True)
    return tmp_path


class TestWorktreeLifecycle:
    def test_create_and_cleanup(self, git_repo):
        wt = workspace.create_worktree(git_repo, tasknum=1)
        assert wt.exists()
        assert (wt / "main.py").exists()

        workspace.cleanup_worktree(git_repo, wt)
        assert not wt.exists()

    def test_get_diff_empty(self, git_repo):
        wt = workspace.create_worktree(git_repo, tasknum=2)
        diff = workspace.get_diff(wt)
        assert diff == ""  # no changes
        workspace.cleanup_worktree(git_repo, wt)

    def test_get_diff_with_changes(self, git_repo):
        wt = workspace.create_worktree(git_repo, tasknum=3)
        (wt / "main.py").write_text("print('modified')\n")
        diff = workspace.get_diff(wt)
        assert "modified" in diff
        workspace.cleanup_worktree(git_repo, wt)

    def test_commit_worktree(self, git_repo):
        wt = workspace.create_worktree(git_repo, tasknum=4)
        (wt / "new_file.py").write_text("x = 1\n")
        sha = workspace.commit_worktree(wt, "test commit")
        assert sha is not None
        workspace.cleanup_worktree(git_repo, wt)

    def test_commit_worktree_no_changes(self, git_repo):
        wt = workspace.create_worktree(git_repo, tasknum=5)
        sha = workspace.commit_worktree(wt, "empty")
        assert sha is None
        workspace.cleanup_worktree(git_repo, wt)


class TestScanCodebase:
    def test_scan_git_repo(self, git_repo):
        summary = workspace.scan_codebase(git_repo)
        assert "main.py" in summary
        assert "README.md" in summary
        assert "print('hello')" in summary

    def test_scan_respects_gitignore(self, git_repo):
        (git_repo / ".gitignore").write_text("secret.txt\n")
        (git_repo / "secret.txt").write_text("password=123\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=str(git_repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "add gitignore"], cwd=str(git_repo), capture_output=True)

        summary = workspace.scan_codebase(git_repo)
        assert "secret.txt" not in summary
        assert "password" not in summary

    def test_scan_prioritizes_source_over_docs(self, git_repo):
        # Source files should appear before doc files
        summary = workspace.scan_codebase(git_repo)
        py_pos = summary.find("main.py")
        md_pos = summary.find("README.md")
        # In Key File Contents, .py should come before .md
        content_section = summary[summary.find("## Key File Contents"):]
        py_content_pos = content_section.find("main.py")
        md_content_pos = content_section.find("README.md")
        assert py_content_pos < md_content_pos

    def test_scan_non_git_dir(self, tmp_path):
        """Falls back to rglob when not a git repo."""
        (tmp_path / "file.py").write_text("x = 1\n")
        summary = workspace.scan_codebase(tmp_path)
        assert "file.py" in summary


class TestMergeResult:
    def test_merge_success(self, git_repo):
        # Add .auto to gitignore so worktree dir doesn't make it "dirty"
        (git_repo / ".gitignore").write_text(".auto/\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=str(git_repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "add gitignore"], cwd=str(git_repo), capture_output=True)

        wt = workspace.create_worktree(git_repo, tasknum=6)
        (wt / "improvement.py").write_text("better = True\n")
        workspace.commit_worktree(wt, "improve")
        result = workspace.merge_worktree(git_repo, wt)
        assert result.success
        assert (git_repo / "improvement.py").exists()
        assert "better = True" in (git_repo / "improvement.py").read_text()
        workspace.cleanup_worktree(git_repo, wt)

    def test_merge_result_summary(self):
        r = workspace.MergeResult(success=False, branch="auto/task-1", main_branch="main",
                                  error="Merge conflict", details="file.py")
        s = r.summary()
        assert "Merge conflict" in s
        assert "auto/task-1" in s
