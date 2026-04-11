# Task: Replace print statements with Python logging

## Problem
The codebase uses ~60 `print()` calls for output. This means:
- No log levels (debug, info, warning, error)
- No way to control verbosity
- No timestamps
- Hard to parse programmatically

## Fix
Replace all `print()` calls with Python's `logging` module. Set up a simple configuration in `auto.py:main()`.

### Logging setup (in auto.py, at the top of main()):
```python
import logging
logging.basicConfig(
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("auto")
```

### Mapping rules:
- `print(f"  Error: ...")` -> `log.error(...)`
- `print(f"  Warning: ...")` -> `log.warning(...)`
- `print(f"  [director] ...")`, `print(f"  [idea-gen] ...")`, etc. -> `log.info(...)`
- `print(f"  [#{tasknum}] CRASH: ...")` -> `log.error(...)`
- `print(f"  [#{tasknum}] Score: ...")` -> `log.info(...)`
- Status/progress messages -> `log.info(...)`
- The decorative `=` and `-` separator lines -> `log.info(...)` (keep them for readability)
- `input()` calls should stay as-is (they're interactive)

### Each module should get its own logger:
```python
# In agents.py, db.py, workspace.py, llm.py:
import logging
log = logging.getLogger(__name__)
```

Replace print() calls in each module with the appropriate log level.

## Files
- `auto/auto.py` (edit - setup + replace prints)
- `auto/agents.py` (edit - replace prints)
- `auto/llm.py` (edit - replace prints)
- `auto/db.py` (edit - replace prints)
- `auto/workspace.py` (no prints currently, just add logger)

## Tests
- Existing tests should still pass (they don't depend on print output)
- If any tests capture stdout, they may need updating to capture logs instead
