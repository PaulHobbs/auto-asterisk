# Task: Make extract_float less prone to false positives

## Problem
In `llm.py` lines 139-145, the last-resort fallback `r"\b(\d+\.?\d*)\b"` matches ANY number in the text. If experiment output contains "Line 42: error" or "timeout after 30s", it extracts `42.0` or `30.0` as the score.

## Fix
Remove the last-resort bare number fallback entirely. The function should only extract a score from:
1. JSON with a `"score"` key (already correct)
2. Explicit `score:` or `Score=` patterns (already correct)
3. Return `None` otherwise

Delete lines 139-145 (the `# Last resort: first standalone number` block).

## Files
- `auto/llm.py` (edit)
- `tests/unit/test_llm.py` (edit - update tests)

## Tests
Update `tests/unit/test_llm.py`:
- `test_standalone_number` should now assert `None` (no longer matches arbitrary numbers)
- Add `test_rejects_line_numbers`: `extract_float("Line 42: error occurred")` returns `None`
- Add `test_rejects_timestamps`: `extract_float("completed in 3.5 seconds")` returns `None`
- Keep existing JSON and `Score:` tests passing
