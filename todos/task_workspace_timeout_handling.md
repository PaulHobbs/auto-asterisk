# Task: Handle TimeoutExpired in workspace._run and centralize timeout constants

## Problem
1. `workspace.py` line 18-27: `_run()` catches non-zero return codes but lets `subprocess.TimeoutExpired` propagate uncaught. Callers must handle it separately, which is inconsistent.
2. Timeout values are hardcoded and scattered: `workspace.py:20` (30s), `agents.py:148` (budget+120), `llm.py:39` (300s), `auto.py:112,118` (120s).

## Fix

### Part 1: Catch TimeoutExpired in _run
In `workspace._run()`, catch `subprocess.TimeoutExpired` and raise `RuntimeError` with a clear message, consistent with the existing error handling pattern:

```python
def _run(cmd: list[str], cwd: Optional[str] = None, check: bool = True, timeout: int = 30) -> str:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Command timed out after {timeout}s: {' '.join(cmd)}"
        )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result.stdout.strip()
```

### Part 2: Move timeout constants to config.py
Add to `config.py`:
```python
GIT_TIMEOUT = int(os.environ.get("AUTO_GIT_TIMEOUT", "30"))
LLM_TIMEOUT = int(os.environ.get("AUTO_LLM_TIMEOUT", "300"))
SETUP_TIMEOUT = int(os.environ.get("AUTO_SETUP_TIMEOUT", "120"))
```
(Use the `_int_env` helper if the config_validation task has been applied, otherwise use bare `int()` for now.)

Update `workspace.py` to use `GIT_TIMEOUT` from config instead of hardcoded `30`.

## Files
- `auto/workspace.py` (edit)
- `auto/config.py` (edit)
- `tests/unit/test_workspace.py` (add a test for timeout handling)
