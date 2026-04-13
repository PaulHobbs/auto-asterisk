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
from auto.quota import PersistentQuotaError, QuotaPolicy
from auto.session_db import (
    SessionDB, SessionRecord, open_or_create_session,
    session_id_for, find_session_by_codebase,
)
from auto.config import (
    QUOTA_CONSECUTIVE_THRESHOLD, QUOTA_WINDOW_SECONDS,
    QUOTA_WINDOW_THRESHOLD, QUOTA_MAX_BACKOFF,
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
    """Create the .auto/ work directory and ensure it's gitignored."""
    work = codebase / WORK_DIR
    work.mkdir(parents=True, exist_ok=True)

    # Ensure work dir and build artifacts are gitignored so they don't
    # dirty the working tree or cause merge conflicts
    gitignore = codebase / ".gitignore"
    ignore_entries = [f"/{WORK_DIR}/", "__pycache__/", "*.pyc"]
    if gitignore.exists():
        content = gitignore.read_text()
    else:
        content = ""
    new_entries = [e for e in ignore_entries if e not in content]
    if new_entries:
        gitignore.write_text(
            content.rstrip("\n") + "\n" + "\n".join(new_entries) + "\n"
        )
        # Commit .gitignore so worktrees inherit it
        subprocess.run(
            ["git", "add", ".gitignore"], capture_output=True, cwd=str(codebase)
        )
        subprocess.run(
            ["git", "commit", "-m", "auto: add .gitignore"],
            capture_output=True, cwd=str(codebase),
        )

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
                _safehouse_cmd += ["--", "bash", "-c", rubric.setup_code]
                result = subprocess.run(
                    _safehouse_cmd,
                    capture_output=True, text=True, cwd=str(codebase), timeout=120,
                )
                # Safehouse failed — fall back to running without sandbox
                if result.returncode != 0 and "sandbox" in (result.stderr or "").lower():
                    log.warning("Safehouse sandbox failed for setup code, retrying without sandbox.")
                    result = subprocess.run(
                        ["bash", "-c", rubric.setup_code],
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

        log.info(f"[#{tasknum}] Actor returncode: {actor_result.get('returncode', 'N/A')}")
        log.info(f"[#{tasknum}] Diff length: {len(diff)} chars")
        if not diff.strip():
            log.warning(f"[#{tasknum}] No file changes detected in worktree!")
            if stderr:
                log.warning(f"[#{tasknum}] Actor stderr (tail): {stderr[-500:]}")
        else:
            log.info(f"[#{tasknum}] Diff preview: {diff[:200]}")

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
                        log.warning(f"[#{tasknum}] Score improved but commit failed — nothing to merge.")
        else:
            log.warning(f"[#{tasknum}] Could not score this experiment.")

    except PersistentQuotaError:
        raise  # Let quota hibernation propagate (finally handles cleanup)

    except Exception as e:
        runtime = time.time() - t0
        db.update_experiment(tasknum, status="crash", stderr=str(e),
                             metadata={**metadata, "runtime_sec": runtime})
        log.error(f"[#{tasknum}] CRASH: {str(e)[:100]}")

    finally:
        workspace.cleanup_worktree(codebase, wt)


def _save_loop_state(session_db, session_id, director_summary):
    """Persist loop state to session DB for resume."""
    if session_db and session_id:
        session_db.set_phase(session_id, "loop", {
            "loop": {"director_summary": director_summary},
        })


def phase_loop(
    db: DB,
    rubric: Rubric,
    codebase: Path,
    time_budget: int,
    max_experiments: int,
    model: str,
    ideas_per_batch: int = IDEAS_PER_BATCH,
    workers: int = 1,
    session_db: SessionDB = None,
    session_id: str = None,
    initial_director_summary: str = None,
) -> None:
    """Phase 3: Main experiment loop."""
    global _shutdown_requested
    log.info("=" * 60)
    log.info("PHASE 3: EXPERIMENT LOOP")
    if workers > 1:
        log.info(f"Workers: {workers}")
    log.info("=" * 60)

    director_summary = initial_director_summary or "No analysis yet. This is the first batch of experiments."

    while True:
        if _shutdown_requested:
            _save_loop_state(session_db, session_id, director_summary)
            break

        current_count = db.count()
        if max_experiments > 0 and current_count >= max_experiments:
            log.info(f"Reached max experiments ({max_experiments}). Stopping.")
            break

        # ── Director (every N experiments) ──────────────────────
        if current_count > 0 and current_count % DIRECTOR_INTERVAL == 0:
            log.info(f"[director] Analyzing {current_count} experiments...")
            try:
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
            except PersistentQuotaError:
                _save_loop_state(session_db, session_id, director_summary)
                _hibernate_session(session_db, session_id)
                return

        # ── Drain persisted idea queue first (for resume) ──────
        ideas = _drain_or_generate_ideas(
            db, rubric, director_summary, ideas_per_batch,
            session_db, session_id,
        )
        if ideas is None:
            # PersistentQuotaError during idea generation
            return

        log.info(f"[idea-gen] Got {len(ideas)} ideas:")
        for i, idea in enumerate(ideas):
            log.info(f"  {i+1}. {idea.title} ({idea.risk} risk)")

        # ── Run experiments (parallel if workers > 1) ──────────
        try:
            if workers <= 1:
                for idea in ideas:
                    if _shutdown_requested:
                        break
                    _run_single_experiment(db, rubric, codebase, idea, time_budget, model)
                    _mark_idea_done(session_db, idea)
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {}
                    for idea in ideas:
                        if _shutdown_requested:
                            break
                        fut = executor.submit(
                            _run_single_experiment, db, rubric, codebase,
                            idea, time_budget, model,
                        )
                        futures[fut] = idea

                    # Wait for all to complete (or shutdown)
                    for fut in concurrent.futures.as_completed(futures):
                        try:
                            fut.result()
                            _mark_idea_done(session_db, futures[fut])
                        except PersistentQuotaError:
                            # Signal other workers to stop
                            _shutdown_requested = True
                            raise
                        except Exception as e:
                            _mark_idea_done(session_db, futures[fut])
                            log.error(f"Worker exception: {e}")

        except PersistentQuotaError:
            _save_loop_state(session_db, session_id, director_summary)
            _hibernate_session(session_db, session_id)
            return

    # Final summary
    log.info("=" * 60)
    log.info("DONE")
    log.info("=" * 60)
    db.print_summary()


def _drain_or_generate_ideas(db, rubric, director_summary, ideas_per_batch,
                              session_db, session_id):
    """Drain persisted idea queue or generate new ideas. Returns ideas or None on quota error."""
    # Check for pending ideas from a previous session (resume case)
    if session_db and session_id:
        pending = session_db.get_pending_ideas(session_id, limit=ideas_per_batch)
        if pending:
            log.info(f"[idea-gen] Resuming {len(pending)} ideas from previous session.")
            ideas = []
            for rec in pending:
                idea = agents.Idea(
                    title=rec.title, description=rec.description,
                    rationale=rec.rationale, risk=rec.risk,
                )
                idea._queue_id = rec.id  # Track queue ID for dispatch marking
                ideas.append(idea)
            return ideas

    # Generate fresh ideas
    log.info(f"[idea-gen] Generating {ideas_per_batch} ideas...")
    try:
        ideas = agents.generate_ideas(db, rubric, director_summary, ideas_per_batch)
    except PersistentQuotaError:
        _save_loop_state(session_db, session_id, director_summary)
        _hibernate_session(session_db, session_id)
        return None

    # Persist to session queue before dispatching (enables resume mid-batch)
    if session_db and session_id:
        session_db.enqueue_ideas(session_id, ideas)
        # Assign queue IDs to ideas so they can be marked dispatched after completion
        pending = session_db.get_pending_ideas(session_id, limit=ideas_per_batch)
        for idea, rec in zip(ideas, pending):
            idea._queue_id = rec.id

    return ideas


def _mark_idea_done(session_db, idea):
    """Mark an idea as dispatched in the session queue after experiment completes."""
    if session_db and hasattr(idea, '_queue_id'):
        session_db.mark_idea_dispatched(idea._queue_id)


def _hibernate_session(session_db, session_id):
    """Mark session as hibernated and log instructions."""
    if session_db and session_id:
        session_db.mark_hibernated(session_id)
    log.error("Persistent quota limit reached. Session hibernated.")
    log.info("Resume with: auto --resume")


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
    parser.add_argument("--provider", choices=["claude", "gemini"],
                        help="LLM provider (default: auto-detect from CLI availability)")
    parser.add_argument("--session-name", "-s",
                        help="Named session for this run (default: auto-derived)")
    parser.add_argument("--list-sessions", action="store_true",
                        help="List all sessions for this codebase and exit")

    args = parser.parse_args()

    # Override provider for the whole process if specified
    if args.provider:
        os.environ["AUTO_PROVIDER"] = args.provider
        # Update module-level PROVIDER in all modules that imported it
        from auto import config
        import importlib
        importlib.reload(config)
        # Re-import into llm and agents so their module-level bindings update
        from auto import llm as _llm_mod, agents as _agents_mod
        _llm_mod.PROVIDER = config.PROVIDER
        _llm_mod.OPUS = config.OPUS
        _llm_mod.SONNET = config.SONNET
        _llm_mod.HAIKU = config.HAIKU
        _agents_mod.PROVIDER = config.PROVIDER
        _agents_mod.SONNET = config.SONNET

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

    # --list-sessions: show sessions for this codebase
    if args.list_sessions:
        _list_sessions(codebase)
        sys.exit(0)

    # Need a task (or --resume with existing rubric)
    if not args.task and not args.resume:
        parser.print_help()
        sys.exit(1)

    # ── Resume flow ─────────────────────────────────────────────
    session_db = None
    session_id = None
    initial_director_summary = None
    resume_phase = None

    if args.resume:
        # Try session-based resume first
        session_db, session_id, resume_phase, initial_director_summary = (
            _resume_session(codebase, args.session_name)
        )

        # Fall back to legacy rubric-only resume
        rubric = db.get_rubric()
        if not rubric:
            log.error("No existing rubric found. Cannot resume.")
            sys.exit(1)
        task = rubric.task_description
        log.info(f"Resuming: {task}")
        log.info(f"Experiments so far: {db.count()}")
    else:
        task = args.task

    # ── Session bringup ─────────────────────────────────────────
    if session_db is None:
        sid = args.session_name or session_id_for(codebase, task)
        session_db, session_record = open_or_create_session(
            session_id=sid,
            codebase_path=codebase,
            task_description=task,
            cli_args=vars(args),
            experiments_db_path=work / DB_FILE,
        )
        session_id = sid

    ideas_per_batch = args.ideas_per_batch

    # Ensure git repo
    workspace.ensure_git_repo(codebase)

    try:
        # Phase 1: Rubric (idempotent — checks DB)
        rubric = phase_rubric(db, task, codebase)
        session_db.set_phase(session_id, "baseline")

        # Phase 2: Baseline (idempotent — checks db.count())
        if resume_phase not in ("loop", "done"):
            phase_baseline(db, rubric, codebase, args.time_budget)
        session_db.set_phase(session_id, "loop")

        # Phase 3: Loop
        phase_loop(db, rubric, codebase, args.time_budget, args.max_experiments,
                   args.model, ideas_per_batch, workers=args.workers,
                   session_db=session_db, session_id=session_id,
                   initial_director_summary=initial_director_summary)

        # Mark complete unless hibernated (phase_loop handles that internally)
        session = session_db.get_session(session_id)
        if session and session.status == "active":
            session_db.mark_complete(session_id)

    except PersistentQuotaError as e:
        log.error(f"Persistent quota error: {e}")
        _hibernate_session(session_db, session_id)

    except Exception:
        if session_db and session_id:
            session_db.mark_crashed(session_id)
        raise

    finally:
        if session_db:
            session_db.close()
        db.close()


def _resume_session(codebase, session_name=None):
    """Attempt session-based resume. Returns (session_db, session_id, phase, director_summary)."""
    if session_name:
        from auto.session_db import session_db_path
        db_path = session_db_path(session_name)
        if db_path.exists():
            sdb = SessionDB(db_path)
            record = sdb.get_session(session_name)
            if record:
                if record.status == "hibernated":
                    log.info(f"Resuming hibernated session {session_name[:8]} (was quota-limited).")
                    sdb.update_session(session_name, status="active")
                phase_data = record.phase_data or {}
                director_summary = phase_data.get("loop", {}).get("director_summary")
                return sdb, session_name, record.current_phase, director_summary
            sdb.close()
    else:
        result = find_session_by_codebase(codebase)
        if result:
            sid, sdb = result
            record = sdb.get_session(sid)
            if record:
                if record.status == "hibernated":
                    log.info(f"Resuming hibernated session {sid[:8]} (was quota-limited).")
                    sdb.update_session(sid, status="active")
                phase_data = record.phase_data or {}
                director_summary = phase_data.get("loop", {}).get("director_summary")
                return sdb, sid, record.current_phase, director_summary
            sdb.close()

    return None, None, None, None


def _list_sessions(codebase):
    """List all sessions for the given codebase."""
    import sqlite3 as _sqlite3
    from auto.session_db import sessions_dir
    sdir = sessions_dir()
    abs_codebase = str(codebase.resolve())
    found = []
    for db_file in sdir.glob("*.db"):
        try:
            conn = _sqlite3.connect(str(db_file))
            conn.row_factory = _sqlite3.Row
            row = conn.execute(
                "SELECT id, status, current_phase, updated_at FROM session WHERE codebase_path = ?",
                (abs_codebase,),
            ).fetchone()
            conn.close()
            if row:
                found.append(dict(row))
        except Exception:
            continue

    if not found:
        print("No sessions found for this codebase.")
        return

    found.sort(key=lambda r: r.get("updated_at", ""), reverse=True)
    print(f"{'ID':<18} {'Status':<12} {'Phase':<10} {'Updated'}")
    print("-" * 60)
    for r in found:
        print(f"{r['id']:<18} {r['status']:<12} {r['current_phase']:<10} {r.get('updated_at', 'N/A')}")


if __name__ == "__main__":
    main()
