"""Tests for quota detection integration in llm.call()."""

import subprocess
import pytest
from unittest.mock import patch, MagicMock
from auto.llm import call, LLMResponse
from auto.quota import QuotaPolicy, PersistentQuotaError, QuotaSignal


@pytest.fixture
def quota_policy():
    return QuotaPolicy(
        consecutive_errors_threshold=2,
        window_seconds=300,
        window_error_threshold=5,
        min_backoff_seconds=1,  # fast for tests
        max_backoff_seconds=2,
    )


def _ok(stdout="success"):
    """Simulate a clean subprocess exit."""
    return (0, stdout, "")


def _quota_err(stderr="429 Too Many Requests"):
    """Simulate a subprocess that exited non-zero with a quota message."""
    return (1, "", stderr)


def _non_quota_err(stderr="FileNotFoundError: bad path"):
    """Simulate a non-quota subprocess failure."""
    return (1, "", stderr)


class TestLLMQuotaDetection:
    @patch("auto.llm.time.sleep")
    @patch("auto.llm._stream_subprocess")
    def test_quota_error_triggers_callback(self, mock_stream, mock_sleep, quota_policy):
        callback = MagicMock()
        mock_stream.side_effect = [
            _quota_err("429 Too Many Requests"),
            _ok("success"),
        ]
        result = call("test", model="test-model", quota_policy=quota_policy,
                      on_quota_error=callback, provider="claude")
        assert result.text == "success"
        callback.assert_called_once()

    @patch("auto.llm.time.sleep")
    @patch("auto.llm._stream_subprocess")
    def test_non_quota_error_no_callback(self, mock_stream, mock_sleep, quota_policy):
        callback = MagicMock()
        mock_stream.side_effect = [
            _non_quota_err(),
            _ok("ok"),
        ]
        result = call("test", model="test-model", quota_policy=quota_policy,
                      on_quota_error=callback, provider="claude")
        callback.assert_not_called()

    @patch("auto.llm.time.sleep")
    @patch("auto.llm._stream_subprocess")
    def test_persistent_quota_raises_after_threshold(self, mock_stream, mock_sleep, quota_policy):
        # threshold=2, so 2 consecutive quota errors should raise
        mock_stream.side_effect = [
            _quota_err("429 rate limit"),
            _quota_err("429 rate limit"),
            _quota_err("429 rate limit"),  # won't reach this
        ]
        with pytest.raises(PersistentQuotaError):
            call("test", model="m", quota_policy=quota_policy, provider="claude")

    @patch("auto.llm.time.sleep")
    @patch("auto.llm._stream_subprocess")
    def test_quota_recovery_resets_count(self, mock_stream, mock_sleep, quota_policy):
        """A successful call after a quota error resets consecutive count."""
        mock_stream.side_effect = [
            _quota_err("429 rate limit"),
            _ok("recovered"),
        ]
        result = call("test", model="m", quota_policy=quota_policy, provider="claude")
        assert result.text == "recovered"

    @patch("auto.llm.time.sleep")
    @patch("auto.llm._stream_subprocess")
    def test_no_quota_policy_uses_standard_backoff(self, mock_stream, mock_sleep):
        """Without quota_policy, existing retry behavior is unchanged."""
        mock_stream.side_effect = [
            _quota_err("429 rate limit"),
            _ok("ok"),
        ]
        result = call("test", model="m", provider="claude")
        assert result.text == "ok"

    @patch("auto.llm.time.sleep")
    @patch("auto.llm._stream_subprocess")
    def test_successful_call_no_quota_tracking(self, mock_stream, mock_sleep, quota_policy):
        mock_stream.return_value = _ok("good")
        result = call("test", model="m", quota_policy=quota_policy, provider="claude")
        assert result.text == "good"

    @patch("auto.llm.time.sleep")
    @patch("auto.llm._stream_subprocess")
    def test_quota_error_on_exit_triggers_backoff(self, mock_stream, mock_sleep, quota_policy):
        """CLI exits with quota error → backoff then retry."""
        mock_stream.side_effect = [
            _quota_err("429 Too Many Requests"),
            _ok("recovered"),
        ]
        callback = MagicMock()
        result = call("test", model="m", quota_policy=quota_policy,
                      on_quota_error=callback, provider="gemini")
        assert result.text == "recovered"
        callback.assert_called_once()
        mock_sleep.assert_called_once()

    @patch("auto.llm.time.sleep")
    @patch("auto.llm._stream_subprocess")
    def test_repeated_quota_errors_raise_after_threshold(self, mock_stream, mock_sleep, quota_policy):
        """Repeated quota exit errors reach the hibernate threshold."""
        mock_stream.side_effect = [
            _quota_err("429 rate limit"),
            _quota_err("429 rate limit"),
            _quota_err("429 rate limit"),  # won't reach
        ]
        with pytest.raises(PersistentQuotaError):
            call("test", model="m", quota_policy=quota_policy, provider="gemini")

    @patch("auto.llm.time.sleep")
    @patch("auto.llm._stream_subprocess")
    def test_timeout_retries_without_quota_policy(self, mock_stream, mock_sleep):
        """Timeouts retry without PersistentQuotaError when no policy is set."""
        mock_stream.side_effect = [
            subprocess.TimeoutExpired(["gemini"], 300),
            _ok("ok"),
        ]
        result = call("test", model="m", provider="gemini")
        assert result.text == "ok"
