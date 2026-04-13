"""Tests for auto.quota — pure functions, no I/O needed."""

import pytest
from datetime import datetime, timedelta

from auto.quota import (
    QuotaSignal, QuotaPolicy, QuotaState, PersistentQuotaError,
    detect_quota_signal, parse_retry_after, should_hibernate, backoff_for_quota,
)


class TestDetectQuotaSignal:
    def test_claude_429_in_stderr(self):
        signal = detect_quota_signal("Error: 429 Too Many Requests")
        assert signal.is_quota_error
        assert signal.pattern_name == "claude_rate_limit"

    def test_claude_rate_limit_message(self):
        signal = detect_quota_signal("Rate limit exceeded. Please retry later.")
        assert signal.is_quota_error

    def test_claude_quota_exceeded(self):
        signal = detect_quota_signal("API quota exceeded for this billing period")
        assert signal.is_quota_error

    def test_claude_overloaded(self):
        signal = detect_quota_signal("The API is currently overloaded. Please try again.")
        assert signal.is_quota_error

    def test_claude_529_error(self):
        signal = detect_quota_signal("529 API Overloaded")
        assert signal.is_quota_error

    def test_gemini_resource_exhausted(self):
        signal = detect_quota_signal("RESOURCE_EXHAUSTED: Quota exceeded")
        assert signal.is_quota_error

    def test_gemini_per_minute_quota(self):
        signal = detect_quota_signal("per minute quota has been exceeded")
        assert signal.is_quota_error

    def test_clean_output_no_signal(self):
        signal = detect_quota_signal("Model output completed successfully")
        assert not signal.is_quota_error
        assert signal.pattern_name is None

    def test_empty_input(self):
        signal = detect_quota_signal("")
        assert not signal.is_quota_error

    def test_quota_in_stdout(self):
        signal = detect_quota_signal("", stdout="429 rate limited")
        assert signal.is_quota_error

    def test_retry_after_extracted(self):
        signal = detect_quota_signal("429 Too Many Requests. Retry-After: 30")
        assert signal.is_quota_error
        assert signal.retry_after_seconds == 30

    def test_usage_limit(self):
        signal = detect_quota_signal("You have hit your usage limit for today")
        assert signal.is_quota_error

    def test_capacity_error(self):
        signal = detect_quota_signal("No capacity available right now")
        assert signal.is_quota_error

    def test_unrelated_error_not_detected(self):
        signal = detect_quota_signal("FileNotFoundError: No such file or directory")
        assert not signal.is_quota_error

    def test_case_insensitive(self):
        signal = detect_quota_signal("RATE LIMIT hit")
        assert signal.is_quota_error


class TestParseRetryAfter:
    def test_standard_format(self):
        assert parse_retry_after("Retry-After: 60") == 60

    def test_with_colon(self):
        assert parse_retry_after("retry after: 120") == 120

    def test_in_longer_text(self):
        assert parse_retry_after("Error 429. Retry-After: 30 seconds remaining") == 30

    def test_not_present(self):
        assert parse_retry_after("Just a normal error message") is None

    def test_zero_returns_none(self):
        assert parse_retry_after("Retry-After: 0") is None

    def test_empty_string(self):
        assert parse_retry_after("") is None


class TestShouldHibernate:
    def test_consecutive_at_threshold(self):
        policy = QuotaPolicy(consecutive_errors_threshold=3)
        state = QuotaState(consecutive_quota_errors=3)
        assert should_hibernate(state, policy)

    def test_consecutive_above_threshold(self):
        policy = QuotaPolicy(consecutive_errors_threshold=3)
        state = QuotaState(consecutive_quota_errors=5)
        assert should_hibernate(state, policy)

    def test_consecutive_below_threshold(self):
        policy = QuotaPolicy(consecutive_errors_threshold=3)
        state = QuotaState(consecutive_quota_errors=2)
        assert not should_hibernate(state, policy)

    def test_windowed_threshold_exceeded(self):
        now = datetime(2024, 1, 1, 12, 0, 0)
        policy = QuotaPolicy(window_seconds=300, window_error_threshold=3, consecutive_errors_threshold=100)
        timestamps = [now - timedelta(seconds=i * 10) for i in range(3)]
        state = QuotaState(consecutive_quota_errors=0, quota_error_timestamps=timestamps)
        assert should_hibernate(state, policy, now=now)

    def test_windowed_old_events_excluded(self):
        now = datetime(2024, 1, 1, 12, 0, 0)
        policy = QuotaPolicy(window_seconds=60, window_error_threshold=3, consecutive_errors_threshold=100)
        # All events are older than the window
        timestamps = [now - timedelta(seconds=120 + i * 10) for i in range(5)]
        state = QuotaState(consecutive_quota_errors=0, quota_error_timestamps=timestamps)
        assert not should_hibernate(state, policy, now=now)

    def test_zero_errors_no_hibernate(self):
        policy = QuotaPolicy()
        state = QuotaState()
        assert not should_hibernate(state, policy)

    def test_either_threshold_triggers(self):
        """If windowed threshold is met but consecutive is not, still hibernate."""
        now = datetime(2024, 1, 1, 12, 0, 0)
        policy = QuotaPolicy(
            consecutive_errors_threshold=100,  # very high
            window_seconds=300,
            window_error_threshold=2,  # very low
        )
        timestamps = [now - timedelta(seconds=10), now - timedelta(seconds=20)]
        state = QuotaState(consecutive_quota_errors=1, quota_error_timestamps=timestamps)
        assert should_hibernate(state, policy, now=now)


class TestBackoffForQuota:
    def test_uses_retry_after_hint(self):
        policy = QuotaPolicy()
        assert backoff_for_quota(1, policy, retry_after_hint=45) == 45

    def test_retry_after_capped_at_max(self):
        policy = QuotaPolicy(max_backoff_seconds=30)
        assert backoff_for_quota(1, policy, retry_after_hint=60) == 30

    def test_exponential_backoff_first_attempt(self):
        policy = QuotaPolicy(min_backoff_seconds=60, backoff_multiplier=2.0)
        assert backoff_for_quota(1, policy) == 60

    def test_exponential_backoff_second_attempt(self):
        policy = QuotaPolicy(min_backoff_seconds=60, backoff_multiplier=2.0)
        assert backoff_for_quota(2, policy) == 120

    def test_exponential_backoff_third_attempt(self):
        policy = QuotaPolicy(min_backoff_seconds=60, backoff_multiplier=2.0)
        assert backoff_for_quota(3, policy) == 240

    def test_backoff_capped_at_max(self):
        policy = QuotaPolicy(min_backoff_seconds=60, backoff_multiplier=2.0, max_backoff_seconds=200)
        assert backoff_for_quota(5, policy) == 200

    def test_zero_retry_after_uses_computed(self):
        policy = QuotaPolicy(min_backoff_seconds=60)
        # retry_after_hint=0 should be treated as not provided (use computed)
        # Actually our implementation checks > 0, so 0 falls through
        result = backoff_for_quota(1, policy, retry_after_hint=0)
        assert result == 60


class TestPersistentQuotaError:
    def test_message(self):
        err = PersistentQuotaError("quota exhausted")
        assert str(err) == "quota exhausted"

    def test_retry_after(self):
        err = PersistentQuotaError("quota exhausted", retry_after=120)
        assert err.retry_after == 120

    def test_is_exception(self):
        with pytest.raises(PersistentQuotaError):
            raise PersistentQuotaError("test")
