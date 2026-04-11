# Task: Enable Parallelism

**Goal:** Run multiple actor experiments concurrently.

## Problem
The current experiment loop in `auto.py` runs experiments sequentially. Since each experiment is isolated in its own git worktree, the system is "embarrassingly parallel." Running experiments one by one is a major bottleneck, especially for long-running benchmarks.

## Solution
1.  Refactor the `phase_loop` in `auto.py` to use a `concurrent.futures.ProcessPoolExecutor` or `ThreadPoolExecutor`.
2.  Manage a pool of available task numbers and worktrees.
3.  Ensure that the `DB` class is thread-safe for concurrent updates (SQLite's WAL mode helps, but explicit locking or a queue-based update system might be needed).
4.  Implement logic to handle experiments finishing out of order.

## Files to Modify
- `auto.py`: Major refactor of the experiment loop.
- `workspace.py`: Ensure worktree creation and cleanup are safe for concurrent use.
- `db.py`: Verify thread-safety of database operations.
