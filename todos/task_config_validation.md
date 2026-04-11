# Task: Add validation for environment variable parsing in config.py

## Problem
`config.py` lines 16-19 use bare `int()` on environment variable values. If a user sets e.g. `AUTO_TIME_BUDGET=abc`, the program crashes with an unhelpful `ValueError` instead of falling back to defaults.

## Fix
Wrap each `int(os.environ.get(...))` call in a helper that catches `ValueError` and falls back to the default with a warning printed to stderr.

Create a helper function like:
```python
def _int_env(name: str, default: str) -> int:
    raw = os.environ.get(name, default)
    try:
        return int(raw)
    except ValueError:
        import sys
        print(f"Warning: invalid value for {name}={raw!r}, using default {default}", file=sys.stderr)
        return int(default)
```

Apply it to all 4 `int()` conversions: `DIRECTOR_INTERVAL`, `IDEAS_PER_BATCH`, `DEFAULT_TIME_BUDGET`, `DEFAULT_MAX_EXPERIMENTS`.

## Files
- `auto/config.py` (edit)

## Tests
Add tests in `tests/unit/test_config.py`:
- Valid int env var is used
- Invalid env var falls back to default
- Missing env var uses default
