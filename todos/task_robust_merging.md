# Task: Robust Merging

**Goal:** Improve the reliability of merging successful experiments.

## Problem
The `merge_worktree` function in `workspace.py` assumes the codebase is on the correct branch and doesn't handle merge conflicts gracefully. If a merge fails, it simply returns `False` and continues, which could lead to missed improvements.

## Solution
1.  Before merging, explicitly verify that the main `codebase` is on the target branch (e.g., `main` or `master`).
2.  Add logic to handle merge conflicts, possibly by:
    - Attempting a rebase of the experiment branch onto the latest HEAD.
    - Notifying the user and pausing for manual intervention.
3.  Ensure that the working directory is clean before attempting a merge.

## Files to Modify
- `workspace.py`: Update `merge_worktree` with more robust checks and conflict handling.
- `auto.py`: Improve reporting when a merge fails.
