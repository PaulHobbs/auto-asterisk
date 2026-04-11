# Task: Validate actor_result before accessing in auto.py

## Problem
In `auto.py` lines 172-176, `actor_result.get(...)` is called on the dict returned by `agents.run_actor()`. While `run_actor` currently always returns a dict, if it were to raise an exception that gets caught by the wrong handler, or if the return type changes, this code would crash with `AttributeError`.

Similarly in `_run_single_experiment` (lines 246-250), the same pattern is used but inside a try/except that would catch the error -- however the error message would be unhelpful.

## Fix
Add a type check after calling `run_actor()` in both `phase_baseline` and `_run_single_experiment`:

In `phase_baseline` (around line 171), add after the `run_actor` call:
```python
if not isinstance(actor_result, dict):
    raise ValueError(f"Actor returned unexpected type: {type(actor_result)}")
```

In `_run_single_experiment` (around line 243), add the same check.

Additionally, in `run_actor` itself (`agents.py`), ensure the function ALWAYS returns a dict even in unusual edge cases. The current code already does this well, but add a defensive final return at the end of the function (after the try/except blocks) as a safety net -- actually this is already handled. So focus on the caller-side validation.

## Files
- `auto/auto.py` (edit - add validation in two places)
- `tests/test_pure_integration.py` (add test for actor returning unexpected result)
