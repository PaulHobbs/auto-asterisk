"""Tests for quota detection integration in llm.call()."""

import subprocess
import pytest
from unittest.mock import patch, MagicMock
from auto.llm import call, LLMResponse
from auto.quota import QuotaPolicy, PersistentQuotaError


@pytest.fixture
def quota_policy():
    return QuotaPolicy(
        consecutive_errors_threshold=2,
        window_seconds=300,
        window_error_threshold=5,
        min_backoff_seconds=1,  # fast for tests
        max_backoff_seconds=2,
    )


def _make_result(returncode=0, stdout="", stderr=""):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestLLMQuotaDetection:
    @patch("auto.llm.time.sleep")
    @patch("auto.llm.subprocess.run")
    def test_quota_error_triggers_callback(self, mock_run, mock_sleep, quota_policy):
        callback = MagicMock()
        mock_run.side_effect = [
            _make_result(1, stderr="429 Too Many Requests"),
            _make_result(0, stdout="success"),
        ]
        result = call("test", model="test-model", quota_policy=quota_policy,
                      on_quota_error=callback, provider="claude")
        assert result.text == "success"
        callback.assert_called_once()

    @patch("auto.llm.time.sleep")
    @patch("auto.llm.subprocess.run")
    def test_non_quota_error_no_callback(self, mock_run, mock_sleep, quota_policy):
        callback = MagicMock()
        mock_run.side_effect = [
            _make_result(1, stderr="FileNotFoundError: bad path"),
            _make_result(0, stdout="ok"),
        ]
        result = call("test", model="test-model", quota_policy=quota_policy,
                      on_quota_error=callback, provider="claude")
        callback.assert_not_called()

    @patch("auto.llm.time.sleep")
    @patch("auto.llm.subprocess.run")
    def test_persistent_quota_raises_after_threshold(self, mock_run, mock_sleep, quota_policy):
        # threshold=2, so 2 consecutive quota errors should raise
        mock_run.side_effect = [
            _make_result(1, stderr="429 rate limit"),
            _make_result(1, stderr="429 rate limit"),
            _make_result(1, stderr="429 rate limit"),  # won't reach this
        ]
        with pytest.raises(PersistentQuotaError):
            call("test", model="m", quota_policy=quota_policy, provider="claude")

    @patch("auto.llm.time.sleep")
    @patch("auto.llm.subprocess.run")
    def test_quota_recovery_resets_count(self, mock_run, mock_sleep, quota_policy):
        """A successful call after a quota error resets consecutive count."""
        mock_run.side_effect = [
            _make_result(1, stderr="429 rate limit"),
            _make_result(0, stdout="recovered"),
        ]
        result = call("test", model="m", quota_policy=quota_policy, provider="claude")
        assert result.text == "recovered"

    @patch("auto.llm.time.sleep")
    @patch("auto.llm.subprocess.run")
    def test_no_quota_policy_uses_standard_backoff(self, mock_run, mock_sleep):
        """Without quota_policy, existing retry behavior is unchanged."""
        mock_run.side_effect = [
            _make_result(1, stderr="429 rate limit"),
            _make_result(0, stdout="ok"),
        ]
        result = call("test", model="m", provider="claude")
        assert result.text == "ok"

    @patch("auto.llm.time.sleep")
    @patch("auto.llm.subprocess.run")
    def test_successful_call_no_quota_tracking(self, mock_run, mock_sleep, quota_policy):
        mock_run.return_value = _make_result(0, stdout="good")
        result = call("test", model="m", quota_policy=quota_policy, provider="claude")
        assert result.text == "good"
