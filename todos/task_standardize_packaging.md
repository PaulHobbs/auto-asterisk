# Task: Standardize Packaging

**Goal:** Use a standard Python project structure and packaging.

## Problem
The project uses `sys.path.insert(0, ...)` hacks in `auto.py` and `tests/test_e2e.py` to manage imports. This is non-standard and makes it difficult to install or distribute the tool.

## Solution
1.  Create a `pyproject.toml` file to define the project's build system and dependencies.
2.  Reorganize the directory structure into a standard package (e.g., move source files into a `src/auto` directory or just ensure the current root is treated as a package).
3.  Remove the `sys.path` manipulation in favor of installing the project in editable mode (`pip install -e .`).
4.  Update scripts to use the installed package.

## Files to Modify
- `pyproject.toml`: (New file)
- `auto.py`: Remove `sys.path` hacks.
- `tests/test_e2e.py`: Remove `sys.path` hacks.
