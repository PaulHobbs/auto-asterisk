"""Centralized configuration for the auto experiment loop.

All constants are overridable via AUTO_-prefixed environment variables.
"""

import os

# ── Model aliases ────────────────────────────────────────────────────────
OPUS = os.environ.get("AUTO_MODEL_OPUS", "claude-opus-4-6")
SONNET = os.environ.get("AUTO_MODEL_SONNET", "claude-sonnet-4-6")
HAIKU = os.environ.get("AUTO_MODEL_HAIKU", "claude-haiku-4-5-20251001")

# ── Orchestrator defaults ────────────────────────────────────────────────
WORK_DIR = os.environ.get("AUTO_WORK_DIR", ".auto")
DB_FILE = os.environ.get("AUTO_DB_FILE", "experiments.db")
DIRECTOR_INTERVAL = int(os.environ.get("AUTO_DIRECTOR_INTERVAL", "5"))
IDEAS_PER_BATCH = int(os.environ.get("AUTO_IDEAS_PER_BATCH", "3"))
DEFAULT_TIME_BUDGET = int(os.environ.get("AUTO_TIME_BUDGET", "300"))
DEFAULT_MAX_EXPERIMENTS = int(os.environ.get("AUTO_MAX_EXPERIMENTS", "0"))
DEFAULT_MODEL = os.environ.get("AUTO_DEFAULT_MODEL", SONNET)
