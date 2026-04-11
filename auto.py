#!/usr/bin/env python3
"""auto — autonomous experiment loop.

Usage:
    ./auto.py "optimize Foo() rpc latency"
    ./auto.py "simplify this codebase" --max-experiments 100
    ./auto.py --results                      # print experiment table
    ./auto.py --resume                       # resume from last run
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Add parent to path so we can import as a package or standalone
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto.db import DB, DirectorEntry, Experiment, Rubric
from auto import agents, llm, workspace


# ── Constants ───────────────────────────────────────────────────────────

WORK_DIR = ".auto"
DB_FILE = "experiments.db"
DIRECTOR_INTERVAL = 5       # run director every N experiments
IDEAS_PER_BATCH = 3         # ideas to generate per batch
DEFAULT_TIME_BUDGET = 300   # seconds per actor run
DEFAULT_MAX_EXPERIMENTS = 0 # 0 = unlimited
DEFAULT_MODEL = "claude-sonnet-4-6"


# ── Signal handling ─────────────────────────────────────────────────────

_shutdown_requested = False


def _signal_handler(signum, frame):
    global _shutdown_requested
    if _shutdown_requested:
        print("\n\nForce quit.")
        sys.exit(1)
    _shutdown_requested = True
    print("\n\nShutdown requested. Finishing current experiment...")


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ── Orchestrator ────────────────────────────────────────────────────────

def init_work_dir(codebase: Path) -> Path:
    """Create the .auto/ work directory."""
    work = codebase / WORK_DIR
    work.mkdir(parents=True, exist_ok=True)
    return work


def phase_rubric(db: DB, task: str, codebase: Path) -> Rubric:
    """Phase 1: Create and approve the rubric."""
    existing = db.get_rubric()
    if existing:
        print(f"\n  Using existing approved rubric (id={existing.id})")
        return existing

    print("\n" + "=" * 60)
    print("  PHASE 1: RUBRIC CREATION")
    print("=" * 60)
    print(f"\n  Task: {task}")
    print("  Scanning codebase...")

    summary = workspace.scan_codebase(codebase)
    print(f"  Scanned {summary.count('###')} files.")
    print("  Calling rubric agent (this may take a minute)...\n")

    rubric = agents.create_rubric(task, summary)

    # Show rubric to user
    print("=" * 60)
    print("  PROPOSED RUBRIC")
    print("=" * 60)
    print(f"\n  Scoring Dimensions:\n{_indent(rubric.scoring_dimensions)}\n")
    print(f"  Judge Prompt:\n{_indent(rubric.judge_prompt)}\n")
    if rubric.setup_code:
        print(f"  Setup Code:\n{_indent(rubric.setup_code)}\n")

    # Ask for approval
    while True:
        choice = input("  Approve this rubric? [y/n/edit]: ").strip().lower()
        if choice == "y":
            break
        elif choice == "n":
            print("  Rubric rejected. Exiting.")
            sys.exit(0)
        elif choice == "edit":
            print("  (Editing not yet implemented. Approve or reject.)")
        else:
            print("  Please enter y, n, or edit.")

    rubric.approved = True
    rubric_id = db.save_rubric(rubric)
    db.approve_rubric(rubric_id)
    rubric.id = rubric_id

    # Run setup code if present
    if rubric.setup_code:
        print("\n  Running setup code...")
        try:
            result = subprocess.run(
                ["bash", "-c", rubric.setup_code],
                capture_output=True, text=True, cwd=str(codebase), timeout=120,
            )
            if result.returncode != 0:
                print(f"  Setup code warning: {result.stderr[:500]}")
            else:
                print("  Setup code completed.")
                # Commit any files created by setup
                subprocess.run(
                    ["git", "add", "-A"], capture_output=True, cwd=str(codebase)
                )
                subprocess.run(
                    ["git", "commit", "-m", "auto: rubric setup code"],
                    capture_output=True, cwd=str(codebase),
                )
        except subprocess.TimeoutExpired:
            print("  Setup code timed out (120s).")

    return rubric


def phase_baseline(db: DB, rubric: Rubric, codebase: Path, time_budget: int) -> None:
    """Phase 2: Run baseline experiment."""
    if db.count() > 0:
        print(f"\n  Baseline already exists (tasknum=0, score={db.get_best_score()})")
        return

    print("\n" + "=" * 60)
    print("  PHASE 2: BASELINE")
    print("=" * 60)
    print("  Running baseline (no changes)...")

    tasknum = 0
    exp = Experiment(
        tasknum=tasknum,
        approach="Baseline: run the codebase as-is with no modifications.",
        status="running",
    )
    db.insert_experiment(exp)

    # Run actor on unchanged codebase
    wt = workspace.create_worktree(codebase, tasknum)
    try:
        actor_result = agents.run_actor(
            worktree_path=wt,
            idea_description=(
                "Run the existing code/tests/benchmarks WITHOUT making any changes. "
                "Report the current metrics as a baseline."
            ),
            best_score=None,
            scoring_dimensions=rubric.scoring_dimensions,
            time_budget=time_budget,
        )

        exp.approach = actor_result.get("approach", exp.approach)
        exp.results = actor_result.get("results", "")
        exp.stdout = actor_result.get("stdout", "")
        exp.stderr = actor_result.get("stderr", "")
        exp.diff = workspace.get_diff(wt)
        exp.status = "success" if actor_result.get("returncode", -1) == 0 else "crash"
    except Exception as e:
        exp.status = "crash"
        exp.stderr = str(e)
    finally:
        workspace.cleanup_worktree(codebase, wt)

    # Score baseline
    db.update_experiment(tasknum, **{
        "approach": exp.approach,
        "results": exp.results,
        "stdout": exp.stdout,
        "stderr": exp.stderr,
        "diff": exp.diff,
        "status": exp.status,
    })

    if exp.status != "crash":
        print("  Scoring baseline...")
        score = agents.score_experiment(rubric, exp)
        if score is not None:
            db.update_experiment(tasknum, score=score, status="judged")
            print(f"  Baseline score: {score:.4f}")
        else:
            print("  Warning: Could not score baseline.")
    else:
        print(f"  Baseline crashed: {exp.stderr[:200]}")


def phase_loop(
    db: DB,
    rubric: Rubric,
    codebase: Path,
    time_budget: int,
    max_experiments: int,
    model: str,
    ideas_per_batch: int = IDEAS_PER_BATCH,
) -> None:
    """Phase 3: Main experiment loop."""
    print("\n" + "=" * 60)
    print("  PHASE 3: EXPERIMENT LOOP")
    print("=" * 60)

    director_summary = "No analysis yet. This is the first batch of experiments."

    while True:
        if _shutdown_requested:
            break

        current_count = db.count()
        if max_experiments > 0 and current_count >= max_experiments:
            print(f"\n  Reached max experiments ({max_experiments}). Stopping.")
            break

        # ── Director (every N experiments) ──────────────────────
        if current_count > 0 and current_count % DIRECTOR_INTERVAL == 0:
            print(f"\n  [director] Analyzing {current_count} experiments...")
            entry = agents.run_director(db, rubric)
            db.save_director_entry(entry)
            director_summary = entry.summary
            print(f"  [director] Summary saved.")

            # Print patterns if available
            if entry.patterns:
                if entry.patterns.get("working"):
                    print(f"  [director] Working: {entry.patterns['working'][:2]}")
                if entry.patterns.get("next_direction"):
                    print(f"  [director] Next: {entry.patterns['next_direction'][:80]}")

        # ── Idea Generation ─────────────────────────────────────
        print(f"\n  [idea-gen] Generating {ideas_per_batch} ideas...")
        ideas = agents.generate_ideas(db, rubric, director_summary, ideas_per_batch)
        print(f"  [idea-gen] Got {len(ideas)} ideas:")
        for i, idea in enumerate(ideas):
            print(f"    {i+1}. {idea.title} ({idea.risk} risk)")

        # ── Run experiments ─────────────────────────────────────
        for idea in ideas:
            if _shutdown_requested:
                break

            tasknum = db.next_tasknum()
            best_score = db.get_best_score()

            print(f"\n  {'─'*56}")
            print(f"  Experiment #{tasknum}: {idea.title}")
            print(f"  {idea.description[:80]}")
            print(f"  Current best: {best_score:.4f}" if best_score is not None else "  Current best: N/A")

            exp = Experiment(
                tasknum=tasknum,
                approach=f"{idea.title}: {idea.description}",
                status="running",
                metadata={"idea_rationale": idea.rationale, "idea_risk": idea.risk},
            )
            db.insert_experiment(exp)

            # Create worktree and run actor
            wt = workspace.create_worktree(codebase, tasknum)
            t0 = time.time()
            try:
                actor_result = agents.run_actor(
                    worktree_path=wt,
                    idea_description=idea.description,
                    best_score=best_score,
                    scoring_dimensions=rubric.scoring_dimensions,
                    time_budget=time_budget,
                    model=model,
                )
                runtime = time.time() - t0

                exp.approach = actor_result.get("approach", exp.approach)
                exp.results = actor_result.get("results", "")
                exp.stdout = actor_result.get("stdout", "")
                exp.stderr = actor_result.get("stderr", "")
                exp.diff = workspace.get_diff(wt)
                exp.status = "success"

                db.update_experiment(tasknum, **{
                    "approach": exp.approach,
                    "results": exp.results,
                    "stdout": exp.stdout,
                    "stderr": exp.stderr,
                    "diff": exp.diff,
                    "status": "success",
                    "metadata": {**exp.metadata, "runtime_sec": runtime},
                })

                # Score
                print(f"  Scoring...")
                score = agents.score_experiment(rubric, exp)
                if score is not None:
                    db.update_experiment(tasknum, score=score, status="judged")
                    improved = best_score is not None and score < best_score

                    marker = " ★ NEW BEST" if improved else ""
                    print(f"  Score: {score:.4f}{marker}")

                    # Merge if improved
                    if improved:
                        workspace.commit_worktree(wt, f"auto: {idea.title}")
                        if workspace.merge_worktree(codebase, wt):
                            print(f"  Merged improvements into main branch.")
                        else:
                            print(f"  Merge conflict, keeping changes in branch only.")
                else:
                    print(f"  Could not score this experiment.")

            except Exception as e:
                runtime = time.time() - t0
                db.update_experiment(tasknum, status="crash", stderr=str(e),
                                     metadata={**exp.metadata, "runtime_sec": runtime})
                print(f"  CRASH: {str(e)[:100]}")

            finally:
                workspace.cleanup_worktree(codebase, wt)

    # Final summary
    print("\n" + "=" * 60)
    print("  DONE")
    print("=" * 60)
    db.print_summary()


# ── CLI ─────────────────────────────────────────────────────────────────

def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.split("\n"))


def main():
    parser = argparse.ArgumentParser(
        description="auto — autonomous experiment loop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  auto "optimize Foo() rpc latency"
  auto "simplify this codebase" --max-experiments 50
  auto --results
  auto --resume
""",
    )
    parser.add_argument("task", nargs="?", help="Task description")
    parser.add_argument("--codebase", "-c", default=".",
                        help="Path to the codebase (default: current directory)")
    parser.add_argument("--max-experiments", "-n", type=int, default=DEFAULT_MAX_EXPERIMENTS,
                        help="Max experiments to run (0 = unlimited)")
    parser.add_argument("--time-budget", "-t", type=int, default=DEFAULT_TIME_BUDGET,
                        help="Time budget per experiment in seconds (default: 300)")
    parser.add_argument("--model", "-m", default=DEFAULT_MODEL,
                        help="Model for actor agents")
    parser.add_argument("--ideas-per-batch", type=int, default=IDEAS_PER_BATCH,
                        help="Ideas to generate per batch")
    parser.add_argument("--results", action="store_true",
                        help="Print experiment results and exit")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from last run")

    args = parser.parse_args()

    codebase = Path(args.codebase).resolve()
    if not codebase.exists():
        print(f"Error: codebase path does not exist: {codebase}")
        sys.exit(1)

    work = init_work_dir(codebase)
    db = DB(work / DB_FILE)

    # --results: just print and exit
    if args.results:
        db.print_summary()
        sys.exit(0)

    # Need a task (or --resume with existing rubric)
    if not args.task and not args.resume:
        parser.print_help()
        sys.exit(1)

    if args.resume:
        rubric = db.get_rubric()
        if not rubric:
            print("Error: No existing rubric found. Cannot resume.")
            sys.exit(1)
        task = rubric.task_description
        print(f"\n  Resuming: {task}")
        print(f"  Experiments so far: {db.count()}")
    else:
        task = args.task

    ideas_per_batch = args.ideas_per_batch

    # Ensure git repo
    workspace.ensure_git_repo(codebase)

    # Phase 1: Rubric
    rubric = phase_rubric(db, task, codebase)

    # Phase 2: Baseline
    phase_baseline(db, rubric, codebase, args.time_budget)

    # Phase 3: Loop
    phase_loop(db, rubric, codebase, args.time_budget, args.max_experiments,
               args.model, ideas_per_batch)

    db.close()


if __name__ == "__main__":
    main()
