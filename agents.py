"""Agent prompts and call functions.

Each agent = one prompt template + one function that calls it and parses output.
No class hierarchy, no agent base class. Just functions.
"""

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import llm
from .config import SONNET
from .db import DB, DirectorEntry, Experiment, Rubric

log = logging.getLogger(__name__)


# ── Rubric Agent ────────────────────────────────────────────────────────

RUBRIC_SYSTEM = """\
You are a rubric designer for an automated experimentation system. Given a task
description and codebase, you design the evaluation criteria and scoring system
that a separate "judge" agent will use to score each experiment.

Your output MUST be a JSON object with these keys:
- scoring_dimensions: A human-readable description of what dimensions to evaluate
  and how they contribute to the final score. Be specific and measurable.
- judge_prompt: A complete prompt that will be given to a judge LLM along with
  experiment results. The judge must output {"score": <float>} where LOWER is
  BETTER (like a loss function). The prompt should be self-contained and explain
  exactly how to score results.
- setup_code: (optional, can be null) A bash script that sets up any test harness,
  benchmark scripts, or measurement tools needed. This runs ONCE before experiments
  begin. Write files to the codebase directory. If the task can be evaluated by
  reading code alone (e.g., simplification), this can be null.

IMPORTANT:
- The judge prompt must produce scores where LOWER = BETTER.
- The judge prompt should handle edge cases: crashes, timeouts, missing metrics.
- For code quality tasks, consider: correctness (tests pass), complexity, LOC, etc.
- For performance tasks, consider: latency, throughput, memory, etc.
- The setup_code should create benchmark/test scripts that actors will run.
- Scores should be normalized to a reasonable range (e.g., 0-100).
"""

RUBRIC_USER = """\
Task: {task_description}

Codebase summary:
{codebase_summary}

Design a rubric for evaluating experiments on this task.
Respond with a JSON object containing: scoring_dimensions, judge_prompt, setup_code.
"""


def create_rubric(task_description: str, codebase_summary: str) -> Rubric:
    """Call the rubric agent to design evaluation criteria."""
    prompt = RUBRIC_USER.format(
        task_description=task_description,
        codebase_summary=codebase_summary,
    )
    response = llm.call(prompt, system=RUBRIC_SYSTEM, model=llm.OPUS, max_tokens=8192)
    data = llm.extract_json(response.text)
    if not data:
        raise ValueError(f"Rubric agent returned non-JSON response:\n{response.text[:500]}")

    return Rubric(
        task_description=task_description,
        scoring_dimensions=data.get("scoring_dimensions", ""),
        judge_prompt=data.get("judge_prompt", ""),
        setup_code=data.get("setup_code"),
    )


# ── Actor Agent ─────────────────────────────────────────────────────────

ACTOR_PROMPT = """\
You are an experiment actor in an automated research loop. Your job is to
implement ONE specific change to the codebase and test it.

## Your Task
{idea_description}

## Current Best Score
{best_score} (lower is better)

## Scoring Rubric
{scoring_dimensions}

## Instructions
1. Read the relevant files in the current directory to understand the code.
2. Implement the change described above. Keep changes minimal and focused.
3. Run any existing tests or benchmarks to verify correctness.
4. After making changes, output your results as a JSON block fenced with
   ```json ... ``` containing:
   - "approach": brief description of what you actually did
   - "results": what happened when you ran/tested it
   - "metrics": any numeric measurements (optional)

IMPORTANT:
- Make changes in the CURRENT DIRECTORY only.
- Keep changes small and reversible.
- If you break something, try to fix it before giving up.
- Always run tests/benchmarks if they exist.
- If the task mentions running a specific command, run it.
"""


def run_actor(
    worktree_path: str | Path,
    idea_description: str,
    best_score: Optional[float],
    scoring_dimensions: str,
    time_budget: int = 300,
    model: str = SONNET,
    max_turns: int = 30,
) -> dict:
    """Run the actor agent as a claude CLI subprocess.

    Returns a dict with keys: approach, results, metrics, stdout, stderr, returncode.
    """
    worktree_path = Path(worktree_path).resolve()

    prompt = ACTOR_PROMPT.format(
        idea_description=idea_description,
        best_score=f"{best_score:.4f}" if best_score is not None else "N/A (baseline)",
        scoring_dimensions=scoring_dimensions,
    )

    _local_overrides = Path(__file__).parent / "safehouse" / "local-overrides.sb"
    cmd = [
        "safehouse",
        f"--append-profile={_local_overrides}",
    ]
    _user_profile = os.environ.get("SAFEHOUSE_APPEND_PROFILE")
    if _user_profile:
        cmd.append(f"--append-profile={_user_profile}")
    cmd += [
        "claude",
        "--print",
        "--dangerously-skip-permissions",
        "--model", model,
        "--max-turns", str(max_turns),
        "-p", prompt,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(worktree_path),
            timeout=time_budget + 120,  # extra buffer for LLM calls
        )
        stdout = result.stdout
        stderr = result.stderr
        returncode = result.returncode
    except subprocess.TimeoutExpired:
        stdout = ""
        stderr = "TIMEOUT: Actor exceeded time budget"
        returncode = -1
    except FileNotFoundError:
        raise RuntimeError(
            "safehouse or claude CLI not found. Install safehouse with: "
            "brew install eugene1g/safehouse/agent-safehouse — "
            "Install claude with: npm install -g @anthropic-ai/claude-code"
        )

    # Parse the actor's JSON output from stdout
    parsed = llm.extract_json(stdout) if stdout else None
    approach = "unknown"
    results = ""
    metrics = {}

    if parsed and isinstance(parsed, dict):
        approach = parsed.get("approach", approach)
        results = parsed.get("results", results)
        metrics = parsed.get("metrics", metrics)
    elif stdout:
        # If no JSON found, use the last ~500 chars as the results summary
        approach = idea_description[:200]
        results = stdout[-2000:] if len(stdout) > 2000 else stdout

    return {
        "approach": approach,
        "results": results,
        "metrics": metrics,
        "stdout": stdout,
        "stderr": stderr,
        "returncode": returncode,
    }


# ── Judge Agent ─────────────────────────────────────────────────────────

JUDGE_SYSTEM = """\
You are a judge in an automated experimentation system. You evaluate experiment
results against a rubric and output a numerical score.

You MUST respond with ONLY a JSON object: {"score": <float>}
where LOWER scores are BETTER.

Do not include any other text, explanation, or commentary.
"""


def score_experiment(
    rubric: Rubric,
    experiment: Experiment,
    max_stdout: int = 3000,
    max_stderr: int = 1000,
) -> Optional[float]:
    """Call the judge agent to score an experiment.

    Returns the score float, or None if scoring fails.
    """
    stdout_truncated = experiment.stdout or ""
    if len(stdout_truncated) > max_stdout:
        stdout_truncated = "...(truncated)...\n" + stdout_truncated[-max_stdout:]

    stderr_truncated = experiment.stderr or ""
    if len(stderr_truncated) > max_stderr:
        stderr_truncated = "...(truncated)...\n" + stderr_truncated[-max_stderr:]

    prompt = f"""{rubric.judge_prompt}

---

## Experiment to Score

**Approach:** {experiment.approach}

**Results:** {experiment.results or 'No results reported'}

**Status:** {experiment.status}

**Stdout:**
```
{stdout_truncated}
```

**Stderr:**
```
{stderr_truncated}
```

**Diff (changes made):**
```
{(experiment.diff or 'No diff available')[:2000]}
```

Score this experiment. Respond with ONLY: {{"score": <float>}}
"""

    for attempt in range(3):
        try:
            response = llm.call(prompt, system=JUDGE_SYSTEM, model=llm.HAIKU)
            score = llm.extract_float(response.text)
            if score is not None:
                return score
        except Exception as e:
            if attempt == 2:
                log.error(f"[judge] Failed to score after 3 attempts: {e}")
                return None
            time.sleep(1)

    return None


# ── Director Agent ──────────────────────────────────────────────────────

DIRECTOR_SYSTEM = """\
You are a research director analyzing experimental results. Your job is to find
patterns in what works and what doesn't, and provide strategic direction for
future experiments.

Output a JSON object with:
- summary: A 2-4 paragraph narrative summary of the research so far. What's been
  tried? What's working? What's failing? What's the current best approach?
- working: A list of 3-5 patterns/approaches that consistently improve scores.
- failing: A list of 3-5 patterns/approaches that consistently hurt scores.
- next_direction: Your top hypothesis for the most promising next direction.
- exploration_vs_exploitation: "explore" if we should try new things, or
  "exploit" if we should refine what works. Include brief reasoning.
"""

DIRECTOR_USER = """\
## Task
{task_description}

## Scoring Rubric
{scoring_dimensions}

## Experiment History (last {n_experiments}, sorted by tasknum)
{experiments_table}

## Best Score So Far
{best_score}

Analyze these results and provide strategic direction.
"""


def run_director(
    db: DB,
    rubric: Rubric,
    limit: int = 20,
) -> DirectorEntry:
    """Summarize experiment history and identify patterns."""
    recent = db.get_recent(limit=limit)
    if not recent:
        return DirectorEntry(
            after_tasknum=0,
            summary="No experiments yet.",
            patterns={},
        )

    # Build a compact table
    lines = ["tasknum | score | status | approach"]
    lines.append("--------|-------|--------|----------")
    for exp in sorted(recent, key=lambda e: e.tasknum):
        score_str = f"{exp.score:.4f}" if exp.score is not None else "N/A"
        approach = exp.approach[:60].replace("\n", " ")
        lines.append(f"{exp.tasknum:>7} | {score_str:>5} | {exp.status:<6} | {approach}")
    table = "\n".join(lines)

    best_score = db.get_best_score()

    prompt = DIRECTOR_USER.format(
        task_description=rubric.task_description,
        scoring_dimensions=rubric.scoring_dimensions,
        n_experiments=len(recent),
        experiments_table=table,
        best_score=f"{best_score:.4f}" if best_score is not None else "N/A",
    )

    response = llm.call(prompt, system=DIRECTOR_SYSTEM, model=llm.SONNET)
    data = llm.extract_json(response.text)

    if data and isinstance(data, dict):
        summary = data.get("summary", response.text[:1000])
        patterns = {
            "working": data.get("working", []),
            "failing": data.get("failing", []),
            "next_direction": data.get("next_direction", ""),
            "exploration_vs_exploitation": data.get("exploration_vs_exploitation", ""),
        }
    else:
        summary = response.text[:1000]
        patterns = {}

    latest_tasknum = max(e.tasknum for e in recent)
    return DirectorEntry(
        after_tasknum=latest_tasknum,
        summary=summary,
        patterns=patterns,
    )


# ── Idea Generation Agent ──────────────────────────────────────────────

IDEAGEN_SYSTEM = """\
You are an experiment planner for an automated research loop. Based on the
director's analysis and past results, propose new experiments to try.

Each idea should be:
- Specific and actionable (not vague like "try different approaches")
- Different from what's already been tried
- Informed by what's working and what's failing

Output a JSON array of ideas. Each idea is an object with:
- title: Short name (5-10 words)
- description: What to do (2-4 sentences, specific enough for an actor to implement)
- rationale: Why this might work (cite patterns from the analysis)
- risk: "low", "medium", or "high"
"""

IDEAGEN_USER = """\
## Task
{task_description}

## Scoring Rubric
{scoring_dimensions}

## Director's Analysis
{director_summary}

## Best Experiments (top {n_best})
{best_table}

## Worst Experiments (bottom {n_worst})
{worst_table}

## Current Best Score
{best_score}

Propose {num_ideas} new experiments to try. Be creative but grounded in the data.
"""


@dataclass
class Idea:
    title: str
    description: str
    rationale: str
    risk: str = "medium"


def generate_ideas(
    db: DB,
    rubric: Rubric,
    director_summary: str,
    num_ideas: int = 3,
) -> list[Idea]:
    """Propose new experiment ideas based on past results."""
    best = db.get_best(n=5)
    worst = db.get_worst(n=5)
    best_score = db.get_best_score()

    def make_table(exps: list[Experiment]) -> str:
        if not exps:
            return "None yet."
        lines = []
        for e in exps:
            score_str = f"{e.score:.4f}" if e.score is not None else "N/A"
            lines.append(f"  #{e.tasknum} (score={score_str}): {e.approach[:80]}")
        return "\n".join(lines)

    prompt = IDEAGEN_USER.format(
        task_description=rubric.task_description,
        scoring_dimensions=rubric.scoring_dimensions,
        director_summary=director_summary,
        n_best=len(best),
        best_table=make_table(best),
        n_worst=len(worst),
        worst_table=make_table(worst),
        best_score=f"{best_score:.4f}" if best_score is not None else "N/A",
        num_ideas=num_ideas,
    )

    response = llm.call(prompt, system=IDEAGEN_SYSTEM, model=llm.SONNET)
    data = llm.extract_json(response.text)

    ideas = []
    if data and isinstance(data, list):
        for item in data[:num_ideas]:
            if isinstance(item, dict):
                ideas.append(Idea(
                    title=item.get("title", "Untitled"),
                    description=item.get("description", ""),
                    rationale=item.get("rationale", ""),
                    risk=item.get("risk", "medium"),
                ))
    elif data and isinstance(data, dict) and "ideas" in data:
        # Handle {"ideas": [...]} wrapper
        for item in data["ideas"][:num_ideas]:
            if isinstance(item, dict):
                ideas.append(Idea(
                    title=item.get("title", "Untitled"),
                    description=item.get("description", ""),
                    rationale=item.get("rationale", ""),
                    risk=item.get("risk", "medium"),
                ))

    if not ideas:
        # Fallback: generate a single generic idea
        ideas.append(Idea(
            title="Explore new approach",
            description=f"Try a different approach to: {rubric.task_description}",
            rationale="No structured ideas could be parsed; trying a general exploration.",
            risk="medium",
        ))

    return ideas
