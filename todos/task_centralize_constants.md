# Task: Centralize Constants

**Goal:** Move hardcoded configurations to a central configuration file.

## Problem
Model names (e.g., `claude-sonnet-4-6`) and other defaults like `DEFAULT_TIME_BUDGET` are scattered across `agents.py`, `llm.py`, and `auto.py`. This makes it tedious to update models or global settings.

## Solution
1.  Create a `config.py` or `constants.py` file to store all global settings, model names, and default values.
2.  Refactor all existing files to import these constants from the central configuration.
3.  (Optional) Allow overriding these constants via environment variables or a `.env` file.

## Files to Modify
- `config.py`: (New file)
- `agents.py`: Use constants from `config.py`.
- `llm.py`: Use constants from `config.py`.
- `auto.py`: Use constants from `config.py`.
