# Task: Add missing unit tests for edge cases

## Problem
Several important edge cases lack test coverage:
1. `extract_json` with strings containing braces/brackets in values
2. `DB.update_experiment` rejects unknown fields
3. `DB.update_experiment` handles metadata dict->JSON serialization
4. `workspace.merge_worktree` when codebase has uncommitted changes (should fail gracefully)
5. `workspace.merge_worktree` with merge conflicts
6. `workspace.cleanup_worktree` on already-cleaned path (idempotent)

## Fix
Add these tests to the existing test files.

### In tests/unit/test_llm.py:
```python
def test_json_with_braces_in_string(self):
    text = '{"code": "if (x) { y(); }"}'
    result = extract_json(text)
    assert result == {"code": "if (x) { y(); }"}

def test_extract_json_with_newlines_in_value(self):
    text = '{"msg": "line1\\nline2"}'
    result = extract_json(text)
    assert result["msg"] == "line1\nline2"
```

### In tests/unit/test_db.py:
```python
def test_update_ignores_unknown_fields(self, db):
    db.insert_experiment(Experiment(tasknum=0, approach="a"))
    db.update_experiment(0, unknown_field="bad")
    got = db.get_experiment(0)
    assert got.approach == "a"  # unchanged, no crash

def test_update_metadata_serialization(self, db):
    db.insert_experiment(Experiment(tasknum=0, approach="a"))
    db.update_experiment(0, metadata={"runtime": 42.5})
    got = db.get_experiment(0)
    assert got.metadata["runtime"] == 42.5

def test_get_all_ordering(self, db):
    for i in [3, 1, 2]:
        db.insert_experiment(Experiment(tasknum=i, approach=f"exp{i}"))
    all_exps = db.get_all()
    assert [e.tasknum for e in all_exps] == [1, 2, 3]

def test_get_best_score_excludes_crashed(self, db):
    db.insert_experiment(Experiment(tasknum=0, approach="a", score=1.0, status="crash"))
    db.insert_experiment(Experiment(tasknum=1, approach="b", score=5.0, status="judged"))
    assert db.get_best_score() == 5.0  # crash excluded
```

### In tests/unit/test_workspace.py:
```python
def test_cleanup_idempotent(self, git_repo):
    wt = workspace.create_worktree(git_repo, tasknum=10)
    workspace.cleanup_worktree(git_repo, wt)
    workspace.cleanup_worktree(git_repo, wt)  # should not raise

def test_merge_with_dirty_worktree(self, git_repo):
    # Make main repo dirty (uncommitted tracked changes)
    (git_repo / "main.py").write_text("dirty = True\n")
    wt = workspace.create_worktree(git_repo, tasknum=11)
    (wt / "new.py").write_text("y = 1\n")
    workspace.commit_worktree(wt, "change")
    result = workspace.merge_worktree(git_repo, wt)
    assert not result.success
    assert "uncommitted" in result.error.lower()
    workspace.cleanup_worktree(git_repo, wt)
    # Restore
    subprocess.run(["git", "checkout", "--", "main.py"], cwd=str(git_repo), capture_output=True)
```

## Files
- `tests/unit/test_llm.py` (edit)
- `tests/unit/test_db.py` (edit)
- `tests/unit/test_workspace.py` (edit)
