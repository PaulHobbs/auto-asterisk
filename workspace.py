"""Git worktree management for isolated experiment execution.

Each experiment runs in its own git worktree so actors can modify files
without affecting the main branch or other concurrent experiments.
"""

import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
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


@dataclass
class MergeResult:
    success: bool
    branch: str = ""
    main_branch: str = ""
    error: str = ""
    details: str = ""

    def summary(self) -> str:
        if self.success:
            return f"Merged {self.branch} into {self.main_branch}"
        parts = [f"Failed to merge {self.branch} into {self.main_branch}"]
        if self.error:
            parts.append(f"Error: {self.error}")
        if self.details:
            parts.append(f"Details: {self.details}")
        return ". ".join(parts)


def merge_worktree(codebase: str | Path, worktree_path: str | Path) -> MergeResult:
    """Merge the worktree's branch back into the main branch.

    Returns a MergeResult with details about what happened.
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

    if main_branch == "HEAD":
        return MergeResult(
            success=False, branch=branch, main_branch=main_branch,
            error="Main codebase is in detached HEAD state",
        )

    # Ensure no uncommitted changes to tracked files (untracked files are OK)
    status = _run(["git", "diff", "--name-only"], cwd=str(codebase), check=False)
    staged = _run(["git", "diff", "--cached", "--name-only"], cwd=str(codebase), check=False)
    if status.strip() or staged.strip():
        return MergeResult(
            success=False, branch=branch, main_branch=main_branch,
            error="Working directory has uncommitted changes to tracked files",
            details=(status.strip() + " " + staged.strip())[:200],
        )

    # Try rebase onto latest HEAD first (reduces merge conflicts)
    main_head = _run(["git", "rev-parse", "HEAD"], cwd=str(codebase))
    rebase_result = subprocess.run(
        ["git", "rebase", main_head],
        capture_output=True, text=True, cwd=str(worktree_path), timeout=30,
    )
    if rebase_result.returncode != 0:
        # Abort failed rebase, will try direct merge
        subprocess.run(
            ["git", "rebase", "--abort"],
            capture_output=True, cwd=str(worktree_path), timeout=10,
        )

    # Attempt merge
    merge_proc = subprocess.run(
        ["git", "merge", branch, "--no-edit", "-m", f"auto: merge {branch}"],
        capture_output=True, text=True, cwd=str(codebase), timeout=30,
    )

    if merge_proc.returncode == 0:
        return MergeResult(success=True, branch=branch, main_branch=main_branch)

    # Merge failed — gather details
    conflict_files = _run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=str(codebase), check=False,
    )
    _run(["git", "merge", "--abort"], cwd=str(codebase), check=False)

    return MergeResult(
        success=False, branch=branch, main_branch=main_branch,
        error="Merge conflict",
        details=f"Conflicting files: {conflict_files}" if conflict_files else merge_proc.stderr[:300],
    )


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


def _git_ls_files(codebase: Path) -> Optional[list[Path]]:
    """List tracked files via git ls-files. Returns None if not a git repo."""
    try:
        output = _run(["git", "ls-files"], cwd=str(codebase), check=True)
        if not output.strip():
            return None
        return [Path(line) for line in output.strip().split("\n") if line]
    except (RuntimeError, subprocess.TimeoutExpired):
        return None


def scan_codebase(codebase: str | Path, max_files: int = 50, max_chars_per_file: int = 3000) -> str:
    """Create a summary of the codebase for the rubric agent.

    Returns a string with the file tree and truncated contents of key files.
    Uses git ls-files when available to respect .gitignore.
    """
    codebase = Path(codebase).resolve()
    parts = []

    # Try git ls-files first, fall back to rglob
    git_files = _git_ls_files(codebase)
    if git_files is not None:
        files = sorted(git_files)
    else:
        exclude = {".git", "__pycache__", "node_modules", ".auto", ".venv", "venv"}
        files = []
        for p in sorted(codebase.rglob("*")):
            if any(ex in p.parts for ex in exclude):
                continue
            if p.is_file():
                files.append(p.relative_to(codebase))

    # File tree
    parts.append("## File Tree\n```")
    for f in files[:max_files * 2]:
        parts.append(str(f))
    if len(files) > max_files * 2:
        parts.append(f"... and {len(files) - max_files * 2} more files")
    parts.append("```\n")

    # Prioritize source code > config > docs
    high_priority = {".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".sh"}
    med_priority = {".yaml", ".yml", ".toml", ".json"}
    low_priority = {".md", ".txt"}

    def file_priority(f: Path) -> int:
        if f.suffix in high_priority:
            return 0
        if f.suffix in med_priority:
            return 1
        if f.suffix in low_priority:
            return 2
        return 3

    key_files = sorted(
        [f for f in files if f.suffix in high_priority | med_priority | low_priority],
        key=file_priority,
    )[:max_files]

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
