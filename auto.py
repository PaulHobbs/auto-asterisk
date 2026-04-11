#!/usr/bin/env python3
"""auto — autonomous experiment loop.

Usage:
    ./auto.py "optimize Foo() rpc latency"
    ./auto.py "simplify this codebase" --max-experiments 100
    ./auto.py --results                      # print experiment table
    ./auto.py --resume                       # resume from last run
"""

import argparse
import concurrent.futures
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from auto.db import DB, DirectorEntry, Experiment, Rubric
from auto import agents, llm, workspace
from auto.config import (
    WORK_DIR, DB_FILE, DIRECTOR_INTERVAL, IDEAS_PER_BATCH,
    DEFAULT_TIME_BUDGET, DEFAULT_MAX_EXPERIMENTS, DEFAULT_MODEL,
)


log = logging.getLogger("auto")


# ── Signal handling ─────────────────────────────────────────────────────

_shutdown_requested = False


def _signal_handler(signum, frame):
    global _shutdown_requested
    if _shutdown_requested:
        log.error("Force quit.")
        sys.exit(1)
    _shutdown_requested = True
    log.info("Shutdown requested. Finishing current experiment...")


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
        log.info(f"Using existing approved rubric (id={existing.id})")
        return existing

    log.info("=" * 60)
    log.info("PHASE 1: RUBRIC CREATION")
    log.info("=" * 60)
    log.info(f"Task: {task}")
    log.info("Scanning codebase...")

    summary = workspace.scan_codebase(codebase)
    log.info(f"Scanned {summary.count('###')} files.")
    log.info("Calling rubric agent (this may take a minute)...")

    rubric = agents.create_rubric(task, summary)

    # Show rubric to user
    log.info("=" * 60)
    log.info("PROPOSED RUBRIC")
    log.info("=" * 60)
    log.info(f"Scoring Dimensions:\n{_indent(rubric.scoring_dimensions)}\n")
    log.info(f"Judge Prompt:\n{_indent(rubric.judge_prompt)}\n")
    if rubric.setup_code:
        log.info(f"Setup Code:\n{_indent(rubric.setup_code)}\n")

    # Ask for approval
    while True:
        choice = input("  Approve this rubric? [y/n/edit]: ").strip().lower()
        if choice == "y":
            break
        elif choice == "n":
            log.info("Rubric rejected. Exiting.")
            sys.exit(0)
        elif choice == "edit":
            log.info("(Editing not yet implemented. Approve or reject.)")
        else:
            log.info("Please enter y, n, or edit.")

    rubric.approved = True
    rubric_id = db.save_rubric(rubric)
    db.approve_rubric(rubric_id)
    rubric.id = rubric_id

    # Run setup code if present (in safehouse sandbox when available)
    if rubric.setup_code:
        log.info("Running setup code...")
        try:
            # Try safehouse first for isolation
            try:
                _local_overrides = Path(__file__).parent / "safehouse" / "local-overrides.sb"
                _safehouse_cmd = ["safehouse", f"--append-profile={_local_overrides}"]
                _user_profile = os.environ.get("SAFEHOUSE_APPEND_PROFILE")
                if _user_profile:
                    _safehouse_cmd.append(f"--append-profile={_user_profile}")
                _safehouse_cmd += ["bash", "-c", rubric.setup_code]
                result = subprocess.run(
                    _safehouse_cmd,
                    capture_output=True, text=True, cwd=str(codebase), timeout=120,
                )
            except FileNotFoundError:
                log.warning("safehouse not found, running setup code without sandbox.")
                result = subprocess.run(
                    ["bash", "-c", rubric.setup_code],
                    capture_output=True, text=True, cwd=str(codebase), timeout=120,
                )

            if result.returncode != 0:
                log.warning(f"Setup code warning: {result.stderr[:500]}")
            else:
                log.info("Setup code completed.")
                # Commit any files created by setup
                subprocess.run(
                    ["git", "add", "-A"], capture_output=True, cwd=str(codebase)
                )
                subprocess.run(
                    ["git", "commit", "-m", "auto: rubric setup code"],
                    capture_output=True, cwd=str(codebase),
                )
        except subprocess.TimeoutExpired:
            log.warning("Setup code timed out (120s).")

    return rubric


def phase_baseline(db: DB, rubric: Rubric, codebase: Path, time_budget: int) -> None:
    """Phase 2: Run baseline experiment."""
    if db.count() > 0:
        log.info(f"Baseline already exists (tasknum=0, score={db.get_best_score()})")
        return

    log.info("=" * 60)
    log.info("PHASE 2: BASELINE")
    log.info("=" * 60)
    log.info("Running baseline (no changes)...")

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
        if not isinstance(actor_result, dict):
            raise ValueError(f"Actor returned unexpected type: {type(actor_result)}")

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
        log.info("Scoring baseline...")
        score = agents.score_experiment(rubric, exp)
        if score is not None:
            db.update_experiment(tasknum, score=score, status="judged")
            log.info(f"Baseline score: {score:.4f}")
        else:
            log.warning("Could not score baseline.")
    else:
        log.error(f"Baseline crashed: {exp.stderr[:200]}")


_merge_lock = threading.Lock()


def _run_single_experiment(
    db: DB,
    rubric: Rubric,
    codebase: Path,
    idea: "agents.Idea",
    time_budget: int,
    model: str,
) -> None:
    """Run a single experiment in its own worktree. Thread-safe."""
    if _shutdown_requested:
        return

    tasknum = db.reserve_tasknum(
        approach=f"{idea.title}: {idea.description}",
        status="running",
    )
    best_score = db.get_best_score()
    metadata = {"idea_rationale": idea.rationale, "idea_risk": idea.risk}

    log.info(f"{'─'*56}")
    log.info(f"Experiment #{tasknum}: {idea.title}")
    log.info(f"{idea.description[:80]}")
    log.info(f"Current best: {best_score:.4f}" if best_score is not None else "Current best: N/A")

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
        if not isinstance(actor_result, dict):
            raise ValueError(f"Actor returned unexpected type: {type(actor_result)}")
        runtime = time.time() - t0

        approach = actor_result.get("approach", idea.description)
        results = actor_result.get("results", "")
        stdout = actor_result.get("stdout", "")
        stderr = actor_result.get("stderr", "")
        diff = workspace.get_diff(wt)

        db.update_experiment(tasknum, **{
            "approach": approach,
            "results": results,
            "stdout": stdout,
            "stderr": stderr,
            "diff": diff,
            "status": "success",
            "metadata": {**metadata, "runtime_sec": runtime},
        })

        # Build an Experiment for scoring
        exp = Experiment(
            tasknum=tasknum, approach=approach, results=results,
            stdout=stdout, stderr=stderr, diff=diff, status="success",
            metadata=metadata,
        )

        # Score
        log.info(f"[#{tasknum}] Scoring...")
        score = agents.score_experiment(rubric, exp)
        if score is not None:
            db.update_experiment(tasknum, score=score, status="judged")

            # Re-check best score under merge lock to avoid TOCTOU race
            with _merge_lock:
                current_best = db.get_best_score()
                improved = current_best is not None and score <= current_best

                marker = " ★ NEW BEST" if improved else ""
                log.info(f"[#{tasknum}] Score: {score:.4f}{marker}")

                if improved:
                    sha = workspace.commit_worktree(wt, f"auto: {idea.title}")
                    if sha:
                        merge_result = workspace.merge_worktree(codebase, wt)
                        if merge_result.success:
                            log.info(f"[#{tasknum}] Merged improvements into main branch.")
                        else:
                            log.error(f"[#{tasknum}] Merge failed: {merge_result.summary()}")
        else:
            log.warning(f"[#{tasknum}] Could not score this experiment.")

    except Exception as e:
        runtime = time.time() - t0
        db.update_experiment(tasknum, status="crash", stderr=str(e),
                             metadata={**metadata, "runtime_sec": runtime})
        log.error(f"[#{tasknum}] CRASH: {str(e)[:100]}")

    finally:
        workspace.cleanup_worktree(codebase, wt)


def phase_loop(
    db: DB,
    rubric: Rubric,
    codebase: Path,
    time_budget: int,
    max_experiments: int,
    model: str,
    ideas_per_batch: int = IDEAS_PER_BATCH,
    workers: int = 1,
) -> None:
    """Phase 3: Main experiment loop."""
    log.info("=" * 60)
    log.info("PHASE 3: EXPERIMENT LOOP")
    if workers > 1:
        log.info(f"Workers: {workers}")
    log.info("=" * 60)

    director_summary = "No analysis yet. This is the first batch of experiments."

    while True:
        if _shutdown_requested:
            break

        current_count = db.count()
        if max_experiments > 0 and current_count >= max_experiments:
            log.info(f"Reached max experiments ({max_experiments}). Stopping.")
            break

        # ── Director (every N experiments) ──────────────────────
        if current_count > 0 and current_count % DIRECTOR_INTERVAL == 0:
            log.info(f"[director] Analyzing {current_count} experiments...")
            entry = agents.run_director(db, rubric)
            db.save_director_entry(entry)
            director_summary = entry.summary
            log.info("[director] Summary saved.")

            # Print patterns if available
            if entry.patterns:
                if entry.patterns.get("working"):
                    log.info(f"[director] Working: {entry.patterns['working'][:2]}")
                if entry.patterns.get("next_direction"):
                    log.info(f"[director] Next: {entry.patterns['next_direction'][:80]}")

        # ── Idea Generation ─────────────────────────────────────
        log.info(f"[idea-gen] Generating {ideas_per_batch} ideas...")
        ideas = agents.generate_ideas(db, rubric, director_summary, ideas_per_batch)
        log.info(f"[idea-gen] Got {len(ideas)} ideas:")
        for i, idea in enumerate(ideas):
            log.info(f"  {i+1}. {idea.title} ({idea.risk} risk)")

        # ── Run experiments (parallel if workers > 1) ──────────
        if workers <= 1:
            for idea in ideas:
                if _shutdown_requested:
                    break
                _run_single_experiment(db, rubric, codebase, idea, time_budget, model)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = []
                for idea in ideas:
                    if _shutdown_requested:
                        break
                    fut = executor.submit(
                        _run_single_experiment, db, rubric, codebase,
                        idea, time_budget, model,
                    )
                    futures.append(fut)

                # Wait for all to complete (or shutdown)
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        fut.result()
                    except Exception as e:
                        log.error(f"Worker exception: {e}")

    # Final summary
    log.info("=" * 60)
    log.info("DONE")
    log.info("=" * 60)
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
    parser.add_argument("--workers", "-w", type=int, default=1,
                        help="Number of parallel workers (default: 1)")

    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        level=logging.INFO,
    )

    codebase = Path(args.codebase).resolve()
    if not codebase.exists():
        log.error(f"codebase path does not exist: {codebase}")
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
            log.error("No existing rubric found. Cannot resume.")
            sys.exit(1)
        task = rubric.task_description
        log.info(f"Resuming: {task}")
        log.info(f"Experiments so far: {db.count()}")
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
               args.model, ideas_per_batch, workers=args.workers)

    db.close()


if __name__ == "__main__":
    main()
