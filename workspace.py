"""Git worktree management for isolated experiment execution.

Each experiment runs in its own git worktree so actors can modify files
without affecting the main branch or other concurrent experiments.
"""

import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional


WORKTREE_DIR = ".auto/worktrees"


def _run(cmd: list[str], cwd: Optional[str] = None, check: bool = True) -> str:
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=cwd, timeout=30
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def ensure_git_repo(codebase: str | Path) -> None:
    """Initialize a git repo if one doesn't exist."""
    codebase = Path(codebase)
    if not (codebase / ".git").exists():
        _run(["git", "init"], cwd=str(codebase))
        _run(["git", "add", "-A"], cwd=str(codebase))
        _run(["git", "commit", "-m", "initial commit (auto)"], cwd=str(codebase))


def get_best_commit(codebase: str | Path) -> str:
    """Get the current HEAD commit SHA."""
    return _run(["git", "rev-parse", "HEAD"], cwd=str(codebase))


def create_worktree(codebase: str | Path, tasknum: int) -> Path:
    """Create a git worktree for an experiment.

    Returns the path to the worktree directory.
    """
    codebase = Path(codebase).resolve()
    worktree_base = codebase / WORKTREE_DIR
    worktree_base.mkdir(parents=True, exist_ok=True)

    branch_name = f"auto/task-{tasknum}-{uuid.uuid4().hex[:6]}"
    worktree_path = worktree_base / f"task-{tasknum}"

    # Clean up if it exists from a previous crashed run
    if worktree_path.exists():
        cleanup_worktree(codebase, worktree_path)

    _run(
        ["git", "worktree", "add", "-b", branch_name, str(worktree_path)],
        cwd=str(codebase),
    )
    return worktree_path


def get_diff(worktree_path: str | Path) -> str:
    """Get the diff of all changes in the worktree vs its base."""
    worktree_path = Path(worktree_path)
    # Stage everything first so we can diff
    _run(["git", "add", "-A"], cwd=str(worktree_path), check=False)
    diff = _run(["git", "diff", "--cached"], cwd=str(worktree_path), check=False)
    return diff


def commit_worktree(worktree_path: str | Path, message: str) -> Optional[str]:
    """Commit all changes in the worktree. Returns commit SHA or None."""
    worktree_path = Path(worktree_path)
    _run(["git", "add", "-A"], cwd=str(worktree_path), check=False)

    # Check if there's anything to commit
    status = _run(["git", "status", "--porcelain"], cwd=str(worktree_path), check=False)
    if not status.strip():
        return None

    _run(
        ["git", "commit", "-m", message],
        cwd=str(worktree_path),
        check=False,
    )
    return _run(["git", "rev-parse", "HEAD"], cwd=str(worktree_path))


def merge_worktree(codebase: str | Path, worktree_path: str | Path) -> bool:
    """Merge the worktree's branch back into the main branch.

    Returns True if merge succeeded.
    """
    codebase = Path(codebase).resolve()
    worktree_path = Path(worktree_path)

    # Get the worktree's branch name
    branch = _run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(worktree_path),
    )

    # Get the main branch name
    main_branch = _run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(codebase),
    )

    try:
        _run(
            ["git", "merge", branch, "--no-edit", "-m", f"auto: merge {branch}"],
            cwd=str(codebase),
        )
        return True
    except RuntimeError:
        # Merge conflict — abort
        _run(["git", "merge", "--abort"], cwd=str(codebase), check=False)
        return False


def cleanup_worktree(codebase: str | Path, worktree_path: str | Path) -> None:
    """Remove a worktree and its branch."""
    codebase = Path(codebase).resolve()
    worktree_path = Path(worktree_path)

    if not worktree_path.exists():
        return

    # Get branch name before removing
    try:
        branch = _run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(worktree_path),
        )
    except (RuntimeError, subprocess.TimeoutExpired):
        branch = None

    # Remove worktree
    _run(
        ["git", "worktree", "remove", str(worktree_path), "--force"],
        cwd=str(codebase),
        check=False,
    )

    # If directory still exists, force remove it
    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)

    # Prune worktree list
    _run(["git", "worktree", "prune"], cwd=str(codebase), check=False)

    # Delete the branch
    if branch and branch.startswith("auto/"):
        _run(
            ["git", "branch", "-D", branch],
            cwd=str(codebase),
            check=False,
        )


def cleanup_all_worktrees(codebase: str | Path) -> None:
    """Remove all auto-created worktrees."""
    codebase = Path(codebase).resolve()
    worktree_base = codebase / WORKTREE_DIR
    if worktree_base.exists():
        for child in worktree_base.iterdir():
            if child.is_dir():
                cleanup_worktree(codebase, child)
        # Remove the worktrees directory itself if empty
        try:
            worktree_base.rmdir()
        except OSError:
            pass
    _run(["git", "worktree", "prune"], cwd=str(codebase), check=False)


def scan_codebase(codebase: str | Path, max_files: int = 50, max_chars_per_file: int = 3000) -> str:
    """Create a summary of the codebase for the rubric agent.

    Returns a string with the file tree and truncated contents of key files.
    """
    codebase = Path(codebase).resolve()
    parts = []

    # File tree (excluding .git, __pycache__, node_modules, .auto)
    exclude = {".git", "__pycache__", "node_modules", ".auto", ".venv", "venv",
               ".auto", "auto"}
    files = []
    for p in sorted(codebase.rglob("*")):
        # Skip excluded directories
        if any(ex in p.parts for ex in exclude):
            continue
        if p.is_file():
            rel = p.relative_to(codebase)
            files.append(rel)

    parts.append("## File Tree\n```")
    for f in files[:max_files * 2]:  # show more in tree than we read
        parts.append(str(f))
    if len(files) > max_files * 2:
        parts.append(f"... and {len(files) - max_files * 2} more files")
    parts.append("```\n")

    # Read key files (prioritize by extension)
    priority_exts = {".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".sh",
                     ".yaml", ".yml", ".toml", ".json", ".md", ".txt"}
    key_files = [f for f in files if f.suffix in priority_exts][:max_files]

    parts.append("## Key File Contents\n")
    for f in key_files:
        full_path = codebase / f
        try:
            content = full_path.read_text(errors="replace")
            if len(content) > max_chars_per_file:
                content = content[:max_chars_per_file] + "\n... (truncated)"
            parts.append(f"### {f}\n```\n{content}\n```\n")
        except (OSError, UnicodeDecodeError):
            continue

    return "\n".join(parts)
