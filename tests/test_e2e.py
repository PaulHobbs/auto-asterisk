#!/usr/bin/env python3
"""End-to-end test for the auto framework.

Creates a temporary project from the fixture (deliberately slow compute.py),
runs the full auto loop with --max-experiments 5, and verifies:
  1. Rubric was created and stored in DB
  2. Baseline was scored
  3. All 5 experiments ran and were scored
  4. At least one experiment improved on the baseline
  5. Git history shows the improvement was merged
  6. Worktrees were cleaned up
  7. The optimized code is actually faster

Usage:
    python -m auto.tests.test_e2e           # full run (requires ANTHROPIC_API_KEY)
    python -m auto.tests.test_e2e --dry-run # just verify fixture + DB setup
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

from auto.db import DB


FIXTURE_DIR = Path(__file__).resolve().parent / "fixture"
LOCAL_OVERRIDES = Path(__file__).resolve().parent.parent / "safehouse" / "local-overrides.sb"
MAX_EXPERIMENTS = 5
TIME_BUDGET = 120  # 2 min per experiment (these are fast benchmarks)


def setup_test_project(tmpdir: Path) -> Path:
    """Copy fixture files into a temp directory and init git."""
    project = tmpdir / "test_project"
    project.mkdir()

    # Copy fixture files
    for f in FIXTURE_DIR.iterdir():
        if f.is_file():
            shutil.copy2(f, project / f.name)

    # Init git repo with local user config so commits work in worktrees too
    subprocess.run(["git", "init"], cwd=str(project), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "auto-test"], cwd=str(project), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "auto@test.com"], cwd=str(project), capture_output=True, check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(project), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial: deliberately slow implementation"],
        cwd=str(project), capture_output=True, check=True,
    )

    return project


def run_baseline_benchmark(project: Path) -> dict:
    """Run benchmark on the unmodified project."""
    result = subprocess.run(
        ["python", "benchmark.py"],
        cwd=str(project), capture_output=True, text=True, timeout=60,
    )
    return json.loads(result.stdout)


def verify_db(project: Path) -> dict:
    """Open the DB and verify expected state."""
    db_path = project / ".auto" / "experiments.db"
    assert db_path.exists(), f"DB not found at {db_path}"

    db = DB(db_path)
    results = {}

    # Check rubric
    rubric = db.get_rubric()
    assert rubric is not None, "No approved rubric found"
    assert rubric.approved, "Rubric not approved"
    assert len(rubric.judge_prompt) > 50, "Judge prompt too short"
    results["rubric"] = True
    print(f"  [ok] Rubric created and approved")
    print(f"       Dimensions: {rubric.scoring_dimensions[:100]}...")

    # Check experiments
    all_exps = db.get_all()
    results["total_experiments"] = len(all_exps)
    print(f"  [ok] {len(all_exps)} experiments in DB")

    # Check baseline
    baseline = db.get_experiment(0)
    assert baseline is not None, "No baseline experiment (tasknum=0)"
    assert baseline.score is not None, "Baseline not scored"
    results["baseline_score"] = baseline.score
    print(f"  [ok] Baseline scored: {baseline.score:.4f}")

    # Check that experiments were scored
    scored = [e for e in all_exps if e.score is not None]
    results["scored_experiments"] = len(scored)
    print(f"  [ok] {len(scored)}/{len(all_exps)} experiments scored")

    # Check for improvements
    best = db.get_best(n=1)
    if best:
        results["best_score"] = best[0].score
        results["best_approach"] = best[0].approach
        improved = best[0].score < baseline.score
        results["improved"] = improved
        if improved:
            delta = baseline.score - best[0].score
            print(f"  [ok] Best score: {best[0].score:.4f} (improved by {delta:.4f})")
            print(f"       Approach: {best[0].approach[:80]}")
        else:
            print(f"  [!!] No improvement over baseline (best={best[0].score:.4f})")
    else:
        results["improved"] = False
        print(f"  [!!] No scored experiments found")

    # Check statuses
    statuses = {}
    for e in all_exps:
        statuses[e.status] = statuses.get(e.status, 0) + 1
    results["statuses"] = statuses
    print(f"  [ok] Statuses: {statuses}")

    # Check director log
    director = db.get_latest_director_entry()
    results["has_director_log"] = director is not None
    if director:
        print(f"  [ok] Director log exists (after tasknum {director.after_tasknum})")

    # Summary table
    db.print_summary()

    db.close()
    return results


def verify_git(project: Path) -> dict:
    """Check git history for merge commits."""
    result = subprocess.run(
        ["git", "log", "--oneline", "-20"],
        cwd=str(project), capture_output=True, text=True,
    )
    commits = result.stdout.strip().split("\n")
    auto_commits = [c for c in commits if "auto:" in c]
    print(f"  [ok] {len(commits)} total commits, {len(auto_commits)} auto commits")
    for c in auto_commits[:5]:
        print(f"       {c}")
    return {"total_commits": len(commits), "auto_commits": len(auto_commits)}


def _run_in_safehouse(cmd: list[str], cwd: str) -> subprocess.CompletedProcess:
    """Run a command inside safehouse with project + user append-profiles."""
    full_cmd = ["safehouse", f"--append-profile={LOCAL_OVERRIDES}"]
    user_profile = os.environ.get("SAFEHOUSE_APPEND_PROFILE")
    if user_profile:
        full_cmd.append(f"--append-profile={user_profile}")
    full_cmd += ["--", *cmd]
    return subprocess.run(full_cmd, capture_output=True, text=True, cwd=cwd, timeout=30)


def verify_safehouse_git(project: Path) -> bool:
    """Verify that git commands work inside safehouse against the project repo."""
    try:
        subprocess.run(["safehouse", "--version"], capture_output=True, timeout=5)
    except FileNotFoundError:
        print("  [skip] safehouse not installed")
        return True  # don't block e2e if safehouse isn't present

    # Quick check: can safehouse apply its sandbox at all?
    probe = _run_in_safehouse(["true"], cwd=str(project))
    if probe.returncode != 0 and "sandbox" in (probe.stderr or "").lower():
        print("  [skip] safehouse sandbox-exec not permitted on this system")
        return True  # don't block e2e if sandbox can't be applied

    checks = [
        (["git", "status", "--porcelain"], "git status"),
        (["git", "log", "--oneline", "-5"], "git log"),
        (["git", "diff"], "git diff"),
        (["git", "rev-parse", "HEAD"], "git rev-parse"),
        (["git", "branch", "--list"], "git branch"),
        (["git", "config", "--global", "--list"], "git config --global"),
    ]

    all_ok = True
    for cmd, label in checks:
        result = _run_in_safehouse(cmd, cwd=str(project))
        if result.returncode != 0:
            print(f"  [FAIL] {label}: exit {result.returncode} — {result.stderr.strip()}")
            all_ok = False
        else:
            print(f"  [ok] {label}")

    return all_ok


def verify_cleanup(project: Path) -> bool:
    """Check that worktrees were cleaned up."""
    worktrees = project / ".auto" / "worktrees"
    if worktrees.exists():
        remaining = list(worktrees.iterdir())
        if remaining:
            print(f"  [!!] {len(remaining)} worktrees not cleaned up: {remaining}")
            return False
    print(f"  [ok] All worktrees cleaned up")
    return True


def verify_benchmark_unmodified(project: Path) -> bool:
    """Verify benchmark.py wasn't tampered with by the actor."""
    original = (FIXTURE_DIR / "benchmark.py").read_text()
    current = (project / "benchmark.py").read_text()
    if original == current:
        print("  [ok] benchmark.py is unmodified")
        return True
    else:
        print("  [FAIL] benchmark.py was modified by the actor!")
        # Show what changed
        import difflib
        diff = difflib.unified_diff(
            original.splitlines(), current.splitlines(),
            fromfile="fixture/benchmark.py", tofile="project/benchmark.py",
            lineterm="",
        )
        for line in list(diff)[:20]:
            print(f"       {line}")
        return False


def verify_performance(project: Path, baseline_latency: float) -> dict:
    """Run benchmark on the (possibly improved) project and compare."""
    result = subprocess.run(
        ["python", "benchmark.py"],
        cwd=str(project), capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        print(f"  [!!] Benchmark failed after optimization: {result.stderr[:200]}")
        return {"improved": False, "error": result.stderr[:200]}

    report = json.loads(result.stdout)
    new_latency = report["latency_ms"]
    speedup = baseline_latency / new_latency if new_latency > 0 else float("inf")
    print(f"  [ok] Final latency: {new_latency:.2f}ms (was {baseline_latency:.2f}ms)")
    print(f"       Speedup: {speedup:.1f}x")
    print(f"       Correctness: {report['correctness']}")
    return {
        "baseline_latency_ms": baseline_latency,
        "final_latency_ms": new_latency,
        "speedup": speedup,
        "correctness": report["correctness"],
    }


def run_e2e(dry_run: bool = False, model: str = None):
    """Run the full end-to-end test."""
    print("\n" + "=" * 60)
    print("  AUTO FRAMEWORK — END-TO-END TEST")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="auto_e2e_") as tmpdir:
        tmpdir = Path(tmpdir)

        # Setup
        print("\n[1/9] Setting up test project...")
        project = setup_test_project(tmpdir)
        print(f"  Project at: {project}")

        # Pre-flight: verify safehouse allows git
        print("\n[2/9] Verifying safehouse git access...")
        safehouse_ok = verify_safehouse_git(project)
        if not safehouse_ok:
            print("  [!!] Safehouse git pre-flight failed — actor may not work correctly")

        # Baseline benchmark
        print("\n[3/9] Running baseline benchmark...")
        baseline = run_baseline_benchmark(project)
        print(f"  Baseline latency: {baseline['latency_ms']:.2f}ms (5k words)")
        print(f"  Baseline latency: {baseline['latency_15k_ms']:.2f}ms (15k words)")
        print(f"  Correctness: {baseline['correctness']}")

        if dry_run:
            print("\n[DRY RUN] Skipping auto loop. Setup verified.")
            return safehouse_ok

        # Run auto loop
        print(f"\n[4/9] Running auto loop (max {MAX_EXPERIMENTS} experiments)...")
        print(f"  Time budget per experiment: {TIME_BUDGET}s")
        print(f"  This will take several minutes...\n")

        t0 = time.time()
        result = subprocess.run(
            [
                sys.executable, "-m", "auto.auto",
                "Make compute.py faster. The benchmark is benchmark.py — run it to "
                "measure latency. The main function is top_k_frequent(). The current "
                "implementation is O(n^2) and uses bubble sort. Optimize it while "
                "keeping the same function signatures and correctness. "
                "IMPORTANT: Do not modify benchmark.py — it contains environment "
                "checks that must remain intact.",
                "--codebase", str(project),
                "--max-experiments", str(MAX_EXPERIMENTS),
                "--time-budget", str(TIME_BUDGET),
                "--ideas-per-batch", "2",
            ] + (["--model", model] if model else []),
            cwd=str(PROJECT_ROOT),
            timeout=MAX_EXPERIMENTS * (TIME_BUDGET + 180) + 300,  # generous timeout
            input="y\n",  # auto-approve rubric
            text=True,
            capture_output=True,
        )
        elapsed = time.time() - t0

        print(f"\n  Auto loop finished in {elapsed:.0f}s")
        if result.returncode != 0:
            print(f"  Return code: {result.returncode}")
            if result.stderr:
                print(f"  Stderr (last 500 chars): {result.stderr[-500:]}")

        # Save full logs to file and print tail
        if result.stderr:
            log_file = tmpdir / "auto_logs.txt"
            log_file.write_text(result.stderr)
            print(f"\n  Full logs saved to: {log_file}")
            # Print merge/commit related lines
            print(f"\n  --- merge/commit/diff log lines ---")
            for line in result.stderr.split("\n"):
                if any(kw in line.lower() for kw in ["merge", "commit", "diff length", "no file changes", "new best", "score improved"]):
                    print(f"  | {line}")
            print(f"  --- end filtered logs ---\n")

        # Print auto.py stdout (last 5000 chars)
        if result.stdout:
            print(f"\n  --- auto.py output (tail) ---")
            for line in result.stdout[-5000:].split("\n"):
                print(f"  | {line}")
            print(f"  --- end output ---\n")

        # Verify DB
        print("\n[5/9] Verifying database state...")
        db_results = verify_db(project)

        # Verify git
        print("\n[6/9] Verifying git history...")
        git_results = verify_git(project)

        # Verify cleanup
        cleanup_ok = verify_cleanup(project)

        # Verify benchmark wasn't tampered with
        print("\n[7/9] Verifying benchmark integrity...")
        benchmark_ok = verify_benchmark_unmodified(project)

        # Post-loop: verify safehouse git still works after mutations
        print("\n[8/9] Verifying safehouse git access (post-loop)...")
        safehouse_post_ok = verify_safehouse_git(project)

        # Verify performance
        print("\n[9/9] Verifying performance improvement...")
        perf_results = verify_performance(project, baseline["latency_ms"])

        # Final report
        print("\n" + "=" * 60)
        print("  RESULTS SUMMARY")
        print("=" * 60)

        checks = [
            ("Safehouse git (pre)", safehouse_ok),
            ("Rubric created", db_results.get("rubric", False)),
            ("Baseline scored", db_results.get("baseline_score") is not None),
            ("Experiments ran", db_results.get("total_experiments", 0) >= MAX_EXPERIMENTS),
            ("Experiments scored", db_results.get("scored_experiments", 0) > 0),
            ("Score improved", db_results.get("improved", False)),
            ("Code actually faster", perf_results.get("speedup", 0) > 1.5),
            ("Correctness maintained", perf_results.get("correctness", False)),
            ("Benchmark unmodified", benchmark_ok),
            ("Worktrees cleaned", cleanup_ok),
            ("Safehouse git (post)", safehouse_post_ok),
        ]

        all_pass = True
        for name, passed in checks:
            icon = "PASS" if passed else "FAIL"
            print(f"  [{icon}] {name}")
            if not passed:
                all_pass = False

        if perf_results.get("speedup"):
            print(f"\n  Speedup: {perf_results['speedup']:.1f}x")
        print(f"  Total experiments: {db_results.get('total_experiments', 0)}")
        print(f"  Total time: {elapsed:.0f}s")

        if all_pass:
            print(f"\n  ALL CHECKS PASSED")
        else:
            print(f"\n  SOME CHECKS FAILED")

        return all_pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="E2E test for auto framework")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only verify fixture setup, don't run auto loop")
    parser.add_argument("--model", default=None,
                        help="Model override (e.g. gemini-2.5-flash, claude-haiku-4-5-20251001)")
    parser.add_argument("--provider", default=None, choices=["claude", "gemini"],
                        help="LLM provider (default: auto-detect)")
    args = parser.parse_args()

    # Set provider for the whole process if specified
    if args.provider:
        os.environ["AUTO_PROVIDER"] = args.provider

    success = run_e2e(dry_run=args.dry_run, model=args.model)
    sys.exit(0 if success else 1)
