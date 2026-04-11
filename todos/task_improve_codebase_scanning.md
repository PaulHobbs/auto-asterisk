# Task: Improve Codebase Scanning

**Goal:** Optimize `scan_codebase` for larger projects and respect configuration.

## Problem
The `scan_codebase` function in `workspace.py` uses a hardcoded exclusion list and may fail to scale for very large repositories, potentially exceeding the LLM's context window or missing important files.

## Solution
1.  Use `git ls-files` to identify files to scan, ensuring that `.gitignore` is automatically respected.
2.  Implement a more sophisticated approach for large codebases, such as:
    - Summarizing file contents instead of reading them in full.
    - Prioritizing files based on their relevance to the task (e.g., by scanning imports or using keyword search).
    - Implementing a simple RAG (Retrieval-Augmented Generation) system for file selection.

## Files to Modify
- `workspace.py`: Update `scan_codebase` to use `git ls-files` and improve summarization logic.
