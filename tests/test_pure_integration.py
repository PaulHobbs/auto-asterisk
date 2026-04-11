"""Pure integration tests — mock LLM calls, test orchestrator logic.

These tests verify that the state machine, database updates, and worktree
cleanup work correctly without hitting any network or incurring API cost.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from auto.db import DB, Experiment, Rubric
from auto import agents, workspace


@pytest.fixture
def git_repo(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(tmp_path), capture_output=True)
    (tmp_path / "main.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path),
                   capture_output=True, check=True)
    return tmp_path


@pytest.fixture
def db(tmp_path):
    d = DB(tmp_path / "test.db")
    yield d
    d.close()


@pytest.fixture
def rubric():
    return Rubric(
        task_description="optimize test",
        scoring_dimensions="lower latency = better",
        judge_prompt="Score the latency. Return {\"score\": <float>}",
        approved=True,
        id=1,
    )


class TestScoreExperiment:
    def test_successful_scoring(self, rubric):
        exp = Experiment(
            tasknum=1, approach="test", results="latency: 50ms",
            status="success", stdout="output", stderr="",
        )
        mock_response = MagicMock()
        mock_response.text = '{"score": 42.0}'

        with patch("auto.agents.llm.call", return_value=mock_response):
            score = agents.score_experiment(rubric, exp)

        assert score == 42.0

    def test_scoring_retries_on_failure(self, rubric):
        exp = Experiment(tasknum=1, approach="test", status="success")
        bad_resp = MagicMock()
        bad_resp.text = "not json"
        good_resp = MagicMock()
        good_resp.text = '{"score": 10.0}'

        with patch("auto.agents.llm.call", side_effect=[bad_resp, good_resp]):
            score = agents.score_experiment(rubric, exp)

        assert score == 10.0

    def test_scoring_returns_none_after_all_retries_fail(self, rubric):
        exp = Experiment(tasknum=1, approach="test", status="success")
        bad = MagicMock()
        bad.text = "garbage"

        with patch("auto.agents.llm.call", return_value=bad):
            score = agents.score_experiment(rubric, exp)

        assert score is None


class TestRunActorMocked:
    def test_actor_parses_json_output(self, git_repo):
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='Some output\n```json\n{"approach": "optimized loop", "results": "2x faster"}\n```\n',
            stderr="",
        )
        with patch("subprocess.run", return_value=mock_result):
            result = agents.run_actor(
                worktree_path=git_repo,
                idea_description="test",
                best_score=10.0,
                scoring_dimensions="test",
                time_budget=60,
            )

        assert result["approach"] == "optimized loop"
        assert result["results"] == "2x faster"
        assert result["returncode"] == 0

    def test_actor_handles_timeout(self, git_repo):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 60)):
            result = agents.run_actor(
                worktree_path=git_repo,
                idea_description="test",
                best_score=None,
                scoring_dimensions="test",
                time_budget=60,
            )

        assert result["returncode"] == -1
        assert "TIMEOUT" in result["stderr"]


class TestWorktreeIntegration:
    def test_experiment_worktree_lifecycle(self, git_repo, db, rubric):
        """Full lifecycle: create worktree, make changes, get diff, commit, merge, cleanup."""
        # Create worktree
        wt = workspace.create_worktree(git_repo, tasknum=1)
        assert wt.exists()

        # Simulate actor making changes
        (wt / "main.py").write_text("x = 2  # optimized\n")
        diff = workspace.get_diff(wt)
        assert "optimized" in diff

        # Commit and merge
        workspace.commit_worktree(wt, "auto: improvement")
        result = workspace.merge_worktree(git_repo, wt)
        assert result.success

        # Verify change is in main
        assert "optimized" in (git_repo / "main.py").read_text()

        # Cleanup
        workspace.cleanup_worktree(git_repo, wt)
        assert not wt.exists()

    def test_cleanup_all_worktrees(self, git_repo):
        wt1 = workspace.create_worktree(git_repo, tasknum=1)
        wt2 = workspace.create_worktree(git_repo, tasknum=2)
        assert wt1.exists()
        assert wt2.exists()

        workspace.cleanup_all_worktrees(git_repo)
        assert not wt1.exists()
        assert not wt2.exists()
