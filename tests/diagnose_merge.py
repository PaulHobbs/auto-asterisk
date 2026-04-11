#!/usr/bin/env python3
"""Diagnose why experiment merges aren't working.

Tests each component in isolation:
1. Safehouse file write persistence
2. Worktree creation/commit/merge pipeline
3. get_diff behavior
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from auto import workspace
from auto.tests.test_e2e import FIXTURE_DIR

LOCAL_OVERRIDES = Path(__file__).resolve().parent.parent / "safehouse" / "local-overrides.sb"


def setup_project(tmpdir: Path) -> Path:
    project = tmpdir / "test_project"
    project.mkdir()
    for f in FIXTURE_DIR.iterdir():
        if f.is_file():
            shutil.copy2(f, project / f.name)
    subprocess.run(["git", "init"], cwd=str(project), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=str(project), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(project), capture_output=True, check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(project), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(project), capture_output=True, check=True)
    return project


def test_safehouse_write(project: Path) -> bool:
    """Test if safehouse allows file writes that persist after exit."""
    print("\n=== Test 1: Safehouse file write persistence ===")

    test_file = project / "safehouse_write_test.txt"
    cmd = [
        "safehouse", f"--append-profile={LOCAL_OVERRIDES}",
    ]
    user_profile = os.environ.get("SAFEHOUSE_APPEND_PROFILE")
    if user_profile:
        cmd.append(f"--append-profile={user_profile}")
    cmd += ["--", "bash", "-c", f"echo 'hello from safehouse' > {test_file}"]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(project), timeout=10)
    print(f"  safehouse exit code: {result.returncode}")
    if result.stderr:
        print(f"  stderr: {result.stderr.strip()[:200]}")

    if test_file.exists():
        content = test_file.read_text().strip()
        print(f"  [PASS] File persisted: '{content}'")
        test_file.unlink()
        return True
    else:
        print(f"  [FAIL] File did NOT persist after safehouse exited!")
        return False


def test_safehouse_write_in_worktree(project: Path) -> bool:
    """Test safehouse writes inside a git worktree (the actual actor scenario)."""
    print("\n=== Test 2: Safehouse write inside worktree ===")

    wt = workspace.create_worktree(project, 99)
    print(f"  Worktree: {wt}")

    test_file = wt / "compute.py"
    original = test_file.read_text()

    # Simulate what the actor does: edit compute.py inside safehouse
    edit_script = f"""
import pathlib
p = pathlib.Path('{test_file}')
content = p.read_text()
content = content.replace('import string', 'import string\\nimport collections')
p.write_text(content)
print('Edit done')
"""
    cmd = ["safehouse", f"--append-profile={LOCAL_OVERRIDES}"]
    user_profile = os.environ.get("SAFEHOUSE_APPEND_PROFILE")
    if user_profile:
        cmd.append(f"--append-profile={user_profile}")
    cmd += ["--", "python3", "-c", edit_script]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(wt), timeout=10)
    print(f"  safehouse exit code: {result.returncode}")
    if result.stdout:
        print(f"  stdout: {result.stdout.strip()}")
    if result.stderr:
        print(f"  stderr: {result.stderr.strip()[:300]}")

    new_content = test_file.read_text()
    changed = new_content != original
    if changed:
        print(f"  [PASS] File modified inside safehouse and change persisted")
    else:
        print(f"  [FAIL] File NOT modified — safehouse may be blocking writes!")

    # Test get_diff
    diff = workspace.get_diff(wt)
    print(f"  get_diff length: {len(diff)} chars")
    if diff.strip():
        print(f"  [PASS] Diff detected")
        print(f"  Diff preview: {diff[:200]}")
    else:
        print(f"  [FAIL] No diff detected!")

    workspace.cleanup_worktree(project, wt)
    return changed


def test_commit_merge(project: Path) -> bool:
    """Test the full commit + merge pipeline with a real file change."""
    print("\n=== Test 3: Commit + merge pipeline (no safehouse) ===")

    wt = workspace.create_worktree(project, 98)
    print(f"  Worktree: {wt}")

    # Directly modify a file (bypass safehouse)
    compute = wt / "compute.py"
    content = compute.read_text()
    content = content.replace(
        "import string",
        "import string\nfrom collections import Counter"
    )
    compute.write_text(content)

    # Test get_diff
    diff = workspace.get_diff(wt)
    print(f"  Diff length: {len(diff)} chars")

    # Test commit
    sha = workspace.commit_worktree(wt, "auto: test optimization")
    if sha:
        print(f"  [PASS] Commit succeeded: {sha[:8]}")
    else:
        print(f"  [FAIL] Commit returned None!")
        workspace.cleanup_worktree(project, wt)
        return False

    # Test merge
    merge_result = workspace.merge_worktree(project, wt)
    if merge_result.success:
        print(f"  [PASS] Merge succeeded")
    else:
        print(f"  [FAIL] Merge failed: {merge_result.summary()}")

    # Check git log
    log_out = subprocess.run(
        ["git", "log", "--oneline", "-5"], cwd=str(project),
        capture_output=True, text=True
    )
    print(f"  Git log:\n    " + "\n    ".join(log_out.stdout.strip().split("\n")))

    workspace.cleanup_worktree(project, wt)
    return merge_result.success


def test_claude_p_mode(project: Path) -> bool:
    """Test what claude -p actually does (needs API key)."""
    print("\n=== Test 4: claude -p tool use test ===")

    wt = workspace.create_worktree(project, 97)

    prompt = "Add 'import os' to the top of compute.py. Just add that one line. Do not change anything else."
    cmd = [
        "safehouse", f"--append-profile={LOCAL_OVERRIDES}",
    ]
    user_profile = os.environ.get("SAFEHOUSE_APPEND_PROFILE")
    if user_profile:
        cmd.append(f"--append-profile={user_profile}")
    cmd += [
        "--", "claude", "-p", prompt,
        "--dangerously-skip-permissions",
        "--max-turns", "5",
    ]

    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(wt), timeout=120
    )
    print(f"  claude exit code: {result.returncode}")
    print(f"  stdout length: {len(result.stdout)} chars")
    print(f"  stderr length: {len(result.stderr)} chars")
    if result.stdout:
        print(f"  stdout (first 500): {result.stdout[:500]}")
    if result.stderr:
        print(f"  stderr (first 500): {result.stderr[:500]}")

    # Check if file was modified
    compute = wt / "compute.py"
    content = compute.read_text()
    changed = "import os" in content
    if changed:
        print(f"  [PASS] claude -p successfully edited file")
    else:
        print(f"  [FAIL] claude -p did NOT edit the file")

    diff = workspace.get_diff(wt)
    print(f"  Diff length: {len(diff)} chars")

    workspace.cleanup_worktree(project, wt)
    return changed


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="diag_") as tmpdir:
        tmpdir = Path(tmpdir)
        project = setup_project(tmpdir)
        print(f"Project: {project}")

        results = {}
        results["safehouse_write"] = test_safehouse_write(project)
        results["safehouse_worktree"] = test_safehouse_write_in_worktree(project)
        results["commit_merge"] = test_commit_merge(project)
        results["claude_p"] = test_claude_p_mode(project)

        print("\n" + "=" * 50)
        print("DIAGNOSIS SUMMARY")
        print("=" * 50)
        for name, passed in results.items():
            print(f"  [{'PASS' if passed else 'FAIL'}] {name}")

        if not results["safehouse_write"] or not results["safehouse_worktree"]:
            print("\n  >> Root cause: Safehouse is blocking file writes!")
            print("     Fix: Update safehouse profile to allow file-write*")
        elif not results["commit_merge"]:
            print("\n  >> Root cause: Git commit/merge pipeline is broken")
        elif not results.get("claude_p", True):
            print("\n  >> Root cause: claude -p mode doesn't use tools")
