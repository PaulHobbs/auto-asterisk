# Task: Isolate Setup Code

**Goal:** Run `rubric.setup_code` in an isolated sandbox.

## Problem
Currently, `auto.py` executes the LLM-generated `rubric.setup_code` directly on the host machine using `subprocess.run(["bash", "-c", rubric.setup_code])`. This poses a significant security risk, as a hallucinating or malicious model could generate code that deletes files, steals credentials, or compromises the host system.

## Solution
1.  Wrap the execution of the setup script within a secure environment like `safehouse` or a Docker container.
2.  Ensure that the environment has access to the codebase worktree to perform necessary setup actions (e.g., creating benchmark scripts).
3.  Handle errors and timeouts within the sandbox to prevent the main orchestrator from hanging.

## Files to Modify
- `auto.py`: Change how `phase_rubric` executes the setup code.
- `agents.py`: (Optional) Update `RUBRIC_SYSTEM` prompt to reflect that setup code runs in a sandbox.
