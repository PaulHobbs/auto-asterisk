"""Quota detection and hibernation policy — pure functions, no I/O."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


# Patterns for detecting quota/rate-limit errors in CLI output
_QUOTA_PATTERNS = [
    ("claude_rate_limit", re.compile(
        r"(rate.limit|too many requests|quota exceeded|usage limit|"
        r"\boverloaded\b|\bcapacity\b|\b529\b|\b429\b)", re.IGNORECASE
    )),
    ("gemini_quota", re.compile(
        r"(RESOURCE_EXHAUSTED|quota.exceeded|rate.limit|"
        r"too many requests|\b429\b|per.minute.quota)", re.IGNORECASE
    )),
]

_RETRY_AFTER_PATTERN = re.compile(r"retry.after[:\s]+(\d+)", re.IGNORECASE)


@dataclass
class QuotaSignal:
    """Result of analyzing subprocess output for quota indicators."""
    is_quota_error: bool
    pattern_name: Optional[str] = None
    retry_after_seconds: Optional[int] = None
    matched_text: Optional[str] = None


@dataclass
class QuotaPolicy:
    """Configurable thresholds for declaring persistent quota problems."""
    consecutive_errors_threshold: int = 3
    window_seconds: int = 300
    window_error_threshold: int = 5
    min_backoff_seconds: int = 60
    max_backoff_seconds: int = 600
    backoff_multiplier: float = 2.0


@dataclass
class QuotaState:
    """In-memory tracking for quota errors within a call sequence."""
    consecutive_quota_errors: int = 0
    quota_error_timestamps: list = field(default_factory=list)  # list[datetime]


class PersistentQuotaError(Exception):
    """Raised when quota detection determines we should hibernate."""
    def __init__(self, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.retry_after = retry_after


def detect_quota_signal(stderr: str, stdout: str = "") -> QuotaSignal:
    """Parse CLI output for quota/rate-limit indicators. Pure function."""
    combined = f"{stderr}\n{stdout}"
    for pattern_name, pattern in _QUOTA_PATTERNS:
        match = pattern.search(combined)
        if match:
            return QuotaSignal(
                is_quota_error=True,
                pattern_name=pattern_name,
                retry_after_seconds=parse_retry_after(combined),
                matched_text=match.group(0),
            )
    return QuotaSignal(is_quota_error=False)


def parse_retry_after(text: str) -> Optional[int]:
    """Extract retry-after seconds from CLI output. Returns None if not found. Pure function."""
    match = _RETRY_AFTER_PATTERN.search(text)
    if match:
        val = int(match.group(1))
        return val if val > 0 else None
    return None


def should_hibernate(state: QuotaState, policy: QuotaPolicy, now: Optional[datetime] = None) -> bool:
    """Returns True if quota state exceeds either consecutive or windowed threshold. Pure function."""
    # Check consecutive threshold
    if state.consecutive_quota_errors >= policy.consecutive_errors_threshold:
        return True

    # Check windowed threshold
    now = now or datetime.utcnow()
    window_start = now - timedelta(seconds=policy.window_seconds)
    recent_count = sum(1 for ts in state.quota_error_timestamps if ts >= window_start)
    if recent_count >= policy.window_error_threshold:
        return True

    return False


def backoff_for_quota(
    consecutive_count: int,
    policy: QuotaPolicy,
    retry_after_hint: Optional[int] = None,
) -> int:
    """Returns seconds to wait before next attempt. Pure function."""
    if retry_after_hint is not None and retry_after_hint > 0:
        return min(retry_after_hint, policy.max_backoff_seconds)

    computed = int(policy.min_backoff_seconds * (policy.backoff_multiplier ** (max(consecutive_count, 1) - 1)))
    return min(computed, policy.max_backoff_seconds)
