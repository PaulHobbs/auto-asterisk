# auto — autonomous experiment loop

`auto` is an autonomous research and development loop that improves codebases through iterative experimentation. Given a task description (e.g., "optimize RPC latency" or "simplify this codebase"), `auto` explores, implements, tests, and evaluates changes in a self-correcting feedback loop.

## How it Works

The system operates in three phases:

1.  **Rubric Creation:** An agent scans your codebase and designs a custom scoring rubric and evaluation harness (setup scripts, benchmarks, etc.) specific to your task.
2.  **Baseline:** The system runs your existing code against the rubric to establish a performance baseline.
3.  **Experiment Loop:**
    *   **Director:** Analyzes the history of experiments to identify successful patterns and strategic directions.
    *   **Idea Generation:** Proposes specific, actionable experiments based on the director's analysis.
    *   **Actor:** Implements a proposed idea in an isolated [Git worktree](https://git-scm.com/docs/git-worktree), runs tests/benchmarks, and reports results.
    *   **Judge:** Scores the experiment's results against the rubric (lower scores are better).
    *   **Merging:** If an experiment improves the best-known score, its changes are automatically merged into the main branch.

## Features

- **Isolated Execution:** Every experiment runs in its own git worktree, ensuring changes never corrupt your main branch unless they are proven improvements.
- **Self-Directing:** The "Director" agent maintains a long-term view of the research, preventing the loop from getting stuck in local optima.
- **Automatic Benchmarking:** Generates its own test harnesses and measurement tools if they don't already exist.
- **Persistent Tracking:** All experiments, logs, diffs, and scores are stored in a local SQLite database (`.auto/experiments.db`).

## Installation

### Prerequisites

- **Python 3.10+**
- **Git**
- **[Safehouse](https://github.com/eugene1g/safehouse):** Required for secure, isolated execution of actor agents.
  ```bash
  brew install eugene1g/safehouse/agent-safehouse
  ```
- **[Claude Code](https://www.npmjs.com/package/@anthropic-ai/claude-code):** The primary agent used for implementing changes.
  ```bash
  npm install -g @anthropic-ai/claude-code
  ```

### Setup

1. Clone this repository.
2. Install dependencies (if any, though currently it mostly relies on external CLIs).
3. Ensure you have an `ANTHROPIC_API_KEY` set in your environment.

## Usage

Start a new experiment loop:
```bash
./auto.py "optimize the performance of the core engine"
```

Limit the number of experiments:
```bash
./auto.py "refactor the API for better readability" --max-experiments 20
```

Resume a previous run:
```bash
./auto.py --resume
```

View the results table:
```bash
./auto.py --results
```

## Configuration

- `--time-budget`: Max seconds per experiment (default: 300).
- `--model`: The LLM to use for the actor (default: `claude-sonnet-4-6`).
- `--ideas-per-batch`: Number of ideas to generate in each cycle.

## Testing

The project includes an end-to-end test suite that verifies the full experiment loop using a provided fixture.

To run the full E2E test (requires `ANTHROPIC_API_KEY`):
```bash
python -m tests.test_e2e
```

To run a dry-run (verifies fixture and DB setup without calling LLMs):
```bash
python -m tests.test_e2e --dry-run
```

## Project Structure

- `auto.py`: Main orchestrator and CLI.
- `agents.py`: LLM agent definitions (Rubric, Actor, Judge, Director, IdeaGen).
- `workspace.py`: Git worktree and codebase management.
- `db.py`: SQLite persistence layer.
- `llm.py`: LLM provider utilities.
