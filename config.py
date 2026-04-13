"""Centralized configuration for the auto experiment loop.

All constants are overridable via AUTO_-prefixed environment variables.
"""

import os
import shutil
import sys
from pathlib import Path


def _int_env(name: str, default: str) -> int:
    """Read an integer from an environment variable, falling back to *default*.

    If the environment variable is set to a non-integer value a warning is
    printed to stderr and the default is returned.
    """
    raw = os.environ.get(name, default)
    try:
        return int(raw)
    except ValueError:
        print(
            f"Warning: invalid value for {name}={raw!r}, using default {default}",
            file=sys.stderr,
        )
        return int(default)


# ── Provider detection ───────────────────────────────────────────────────
# AUTO_PROVIDER = "claude" | "gemini"  (auto-detected from CLI availability)
def _detect_provider() -> str:
    explicit = os.environ.get("AUTO_PROVIDER", "").lower()
    if explicit in ("claude", "gemini"):
        return explicit
    # Auto-detect: prefer claude if available, fall back to gemini
    if shutil.which("claude"):
        return "claude"
    if shutil.which("gemini"):
        return "gemini"
    return "claude"  # default; will fail at runtime with a clear error


PROVIDER = _detect_provider()

# ── Model aliases ────────────────────────────────────────────────────────
if PROVIDER == "gemini":
    OPUS = os.environ.get("AUTO_MODEL_OPUS", "gemini-3.1-pro-preview")
    SONNET = os.environ.get("AUTO_MODEL_SONNET", "gemini-3-flash-preview")
    HAIKU = os.environ.get("AUTO_MODEL_HAIKU", "gemini-3.1-flash-lite-preview")
else:
    OPUS = os.environ.get("AUTO_MODEL_OPUS", "claude-opus-4-6")
    SONNET = os.environ.get("AUTO_MODEL_SONNET", "claude-sonnet-4-6")
    HAIKU = os.environ.get("AUTO_MODEL_HAIKU", "claude-haiku-4-5-20251001")

# ── Orchestrator defaults ────────────────────────────────────────────────
WORK_DIR = os.environ.get("AUTO_WORK_DIR", ".auto")
DB_FILE = os.environ.get("AUTO_DB_FILE", "experiments.db")
DIRECTOR_INTERVAL = _int_env("AUTO_DIRECTOR_INTERVAL", "5")
IDEAS_PER_BATCH = _int_env("AUTO_IDEAS_PER_BATCH", "3")
DEFAULT_TIME_BUDGET = _int_env("AUTO_TIME_BUDGET", "300")
DEFAULT_MAX_EXPERIMENTS = _int_env("AUTO_MAX_EXPERIMENTS", "0")
DEFAULT_MODEL = os.environ.get("AUTO_DEFAULT_MODEL", SONNET)

# ── Timeout constants ─────────────────────────────────────────────────────
GIT_TIMEOUT = _int_env("AUTO_GIT_TIMEOUT", "30")
LLM_TIMEOUT = _int_env("AUTO_LLM_TIMEOUT", "300")
SETUP_TIMEOUT = _int_env("AUTO_SETUP_TIMEOUT", "120")

# ── Session store ─────────────────────────────────────────────────────────
SESSIONS_DIR = Path(os.environ.get(
    "AUTO_SESSIONS_DIR",
    str(Path.home() / ".auto-asterisk" / "sessions"),
))

# ── Quota policy defaults ─────────────────────────────────────────────────
QUOTA_CONSECUTIVE_THRESHOLD = _int_env("AUTO_QUOTA_CONSECUTIVE", "3")
QUOTA_WINDOW_SECONDS = _int_env("AUTO_QUOTA_WINDOW_SECONDS", "300")
QUOTA_WINDOW_THRESHOLD = _int_env("AUTO_QUOTA_WINDOW_ERRORS", "5")
QUOTA_MAX_BACKOFF = _int_env("AUTO_QUOTA_MAX_BACKOFF", "600")
