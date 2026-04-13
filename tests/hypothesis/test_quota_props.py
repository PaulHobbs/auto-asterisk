"""Hypothesis property tests for auto.quota."""

import pytest
from datetime import datetime, timedelta
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from auto.quota import (
    detect_quota_signal, parse_retry_after, should_hibernate,
    backoff_for_quota, QuotaPolicy, QuotaState, QuotaSignal,
)


@given(st.text(min_size=0, max_size=2000))
def test_detect_quota_signal_never_raises(text):
    """detect_quota_signal is total — never raises on any input."""
    signal = detect_quota_signal(text)
    assert isinstance(signal, QuotaSignal)
    assert isinstance(signal.is_quota_error, bool)


@given(st.text(min_size=0, max_size=2000))
def test_detect_quota_signal_with_stdout_never_raises(text):
    signal = detect_quota_signal("", stdout=text)
    assert isinstance(signal, QuotaSignal)


@given(st.text(min_size=0, max_size=1000))
def test_parse_retry_after_returns_positive_or_none(text):
    """parse_retry_after never returns negative numbers."""
    result = parse_retry_after(text)
    assert result is None or result > 0


@given(
    consecutive=st.integers(0, 100),
    threshold=st.integers(1, 100),
)
def test_hibernate_decision_is_deterministic(consecutive, threshold):
    """Same state + policy always yields same decision."""
    policy = QuotaPolicy(consecutive_errors_threshold=threshold)
    state = QuotaState(consecutive_quota_errors=consecutive)
    now = datetime(2024, 1, 1, 12, 0, 0)
    result1 = should_hibernate(state, policy, now=now)
    result2 = should_hibernate(state, policy, now=now)
    assert result1 == result2


@given(
    consecutive=st.integers(0, 100),
    threshold=st.integers(1, 100),
)
def test_hibernate_consecutive_matches_threshold_semantics(consecutive, threshold):
    """Hibernate iff consecutive >= threshold (ignoring window for this test)."""
    policy = QuotaPolicy(
        consecutive_errors_threshold=threshold,
        window_error_threshold=9999,  # disable window check
    )
    state = QuotaState(consecutive_quota_errors=consecutive)
    now = datetime(2024, 1, 1, 12, 0, 0)
    result = should_hibernate(state, policy, now=now)
    assert result == (consecutive >= threshold)


@given(
    count=st.integers(1, 20),
    min_backoff=st.integers(1, 120),
    max_backoff=st.integers(120, 3600),
    multiplier=st.floats(1.0, 4.0, allow_nan=False, allow_infinity=False),
)
def test_backoff_always_within_bounds(count, min_backoff, max_backoff, multiplier):
    """Backoff is always between min and max."""
    policy = QuotaPolicy(
        min_backoff_seconds=min_backoff,
        max_backoff_seconds=max_backoff,
        backoff_multiplier=multiplier,
    )
    result = backoff_for_quota(count, policy)
    assert min_backoff <= result <= max_backoff


@given(
    count=st.integers(1, 20),
    hint=st.integers(1, 3600),
    max_backoff=st.integers(1, 3600),
)
def test_backoff_with_hint_capped(count, hint, max_backoff):
    """When retry_after_hint is given, result is min(hint, max)."""
    policy = QuotaPolicy(max_backoff_seconds=max_backoff)
    result = backoff_for_quota(count, policy, retry_after_hint=hint)
    assert result == min(hint, max_backoff)


@given(
    n_events=st.integers(0, 50),
    window_seconds=st.integers(1, 3600),
    threshold=st.integers(1, 50),
)
def test_window_count_never_exceeds_total(n_events, window_seconds, threshold):
    """When all events fall strictly inside the window, hibernate iff n_events >= threshold."""
    now = datetime(2024, 6, 15, 12, 0, 0)
    # Space events so all fit comfortably inside the window.
    # Each event is placed at now - (i * spacing) where spacing ensures the
    # last event is at most window_seconds/2 seconds ago.
    if n_events > 1:
        spacing = max(0.0, (window_seconds / 2) / n_events)
    else:
        spacing = 0.0
    timestamps = [now - timedelta(seconds=i * spacing) for i in range(n_events)]
    policy = QuotaPolicy(
        window_seconds=window_seconds,
        window_error_threshold=threshold,
        consecutive_errors_threshold=9999,
    )
    state = QuotaState(
        consecutive_quota_errors=0,
        quota_error_timestamps=timestamps,
    )
    result = should_hibernate(state, policy, now=now)
    assert result == (n_events >= threshold)
