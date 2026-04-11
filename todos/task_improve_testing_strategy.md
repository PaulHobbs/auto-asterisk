# Task: Improve Testing Strategy (Pure & Dirty Integration)

**Goal:** Implement a dual-layered integration testing strategy to balance cost, speed, and real-world validation.

## Problem
The current testing is binary: either a "dry run" that does very little, or a full E2E run that is expensive and slow. There is no middle ground to verify the orchestrator's logic without an API key, nor a "cheap" way to verify the full stack with a real LLM.

## Solution

### 1. "Pure" Integration Tests (Fast & Free)
- **Orchestrator Logic:** Test `auto.py` by mocking `llm.call` and the `safehouse/claude` subprocess calls.
- **Mock Scenarios:** Create "Golden Files" or mock responses that simulate:
    - A successful experiment that improves the score.
    - A "crash" where the actor returns a non-zero exit code.
    - A "no improvement" scenario.
    - A merge conflict.
- **Goal:** Verify that the state machine, database updates, and worktree cleanup work correctly under various conditions without hitting any network or incurring cost.

### 2. "Dirty" Integration Tests (Cheap E2E)
- **Haiku-Powered E2E:** Update `test_e2e.py` to support a `--model` flag, allowing it to run against `claude-3-5-haiku` (or the latest available Haiku).
- **Regression Suite:** Use these "dirty" tests to verify that prompts haven't regressed and that the tool-use loop (via `safehouse`) still functions correctly with a smaller model.

### 3. Surgical Unit Tests
- Focus unit tests strictly on high-risk logic where they catch real bugs rather than acting as change detectors:
    - **llm.py:** Robustness of `extract_json` against hallucinations and messy markdown.
    - **workspace.py:** Parsing of git output and path handling for worktrees.
    - **db.py:** Complex queries for "best score" and "recent experiments".

## Files to Modify/Create
- `tests/test_pure_integration.py`: (New) Mock-based orchestrator tests.
- `tests/test_e2e.py`: Add support for cheap model overrides and better status reporting.
- `tests/unit/`: (New directory) For surgical tests of parsing/logic.
- `conftest.py`: (Optional) Setup pytest fixtures for mocking LLM calls globally.
