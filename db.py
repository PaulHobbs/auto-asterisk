"""SQLite database for experiment tracking.

Schema:
  rubric       — one row, stores the approved scoring rubric
  experiments  — one row per experiment, with score, status, diff, etc.
  director_log — periodic summaries of patterns found
"""

import json
import logging
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class Rubric:
    task_description: str
    scoring_dimensions: str
    judge_prompt: str
    setup_code: Optional[str] = None
    approved: bool = False
    id: Optional[int] = None
    created_at: Optional[str] = None


@dataclass
class Experiment:
    tasknum: int
    approach: str
    parent_tasknum: Optional[int] = None
    results: Optional[str] = None
    score: Optional[float] = None
    status: str = "pending"
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    diff: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    created_at: Optional[str] = None


@dataclass
class DirectorEntry:
    after_tasknum: int
    summary: str
    patterns: dict = field(default_factory=dict)
    id: Optional[int] = None
    created_at: Optional[str] = None


class DB:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS rubric (
                id INTEGER PRIMARY KEY,
                created_at TEXT DEFAULT (datetime('now')),
                task_description TEXT NOT NULL,
                scoring_dimensions TEXT NOT NULL,
                judge_prompt TEXT NOT NULL,
                setup_code TEXT,
                approved INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS experiments (
                tasknum INTEGER PRIMARY KEY,
                parent_tasknum INTEGER,
                created_at TEXT DEFAULT (datetime('now')),
                approach TEXT NOT NULL,
                results TEXT,
                score REAL,
                status TEXT DEFAULT 'pending',
                stdout TEXT,
                stderr TEXT,
                diff TEXT,
                metadata TEXT,
                FOREIGN KEY(parent_tasknum) REFERENCES experiments(tasknum)
            );

            CREATE TABLE IF NOT EXISTS director_log (
                id INTEGER PRIMARY KEY,
                created_at TEXT DEFAULT (datetime('now')),
                after_tasknum INTEGER NOT NULL,
                summary TEXT NOT NULL,
                patterns_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_experiments_score
                ON experiments(score) WHERE score IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_experiments_status
                ON experiments(status);
        """)
        self.conn.commit()

    # ── Rubric ──────────────────────────────────────────────

    def save_rubric(self, rubric: Rubric) -> int:
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO rubric (task_description, scoring_dimensions,
                   judge_prompt, setup_code, approved)
                   VALUES (?, ?, ?, ?, ?)""",
                (rubric.task_description, rubric.scoring_dimensions,
                 rubric.judge_prompt, rubric.setup_code, int(rubric.approved)),
            )
            self.conn.commit()
            return cur.lastrowid

    def get_rubric(self) -> Optional[Rubric]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM rubric WHERE approved = 1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return Rubric(
            id=row["id"],
            created_at=row["created_at"],
            task_description=row["task_description"],
            scoring_dimensions=row["scoring_dimensions"],
            judge_prompt=row["judge_prompt"],
            setup_code=row["setup_code"],
            approved=bool(row["approved"]),
        )

    def has_rubric(self) -> bool:
        return self.get_rubric() is not None

    def approve_rubric(self, rubric_id: int):
        with self._lock:
            self.conn.execute(
                "UPDATE rubric SET approved = 1 WHERE id = ?", (rubric_id,)
            )
            self.conn.commit()

    # ── Experiments ─────────────────────────────────────────

    def next_tasknum(self) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(tasknum), -1) + 1 AS next FROM experiments"
            ).fetchone()
            return row["next"]

    def reserve_tasknum(self, approach: str, status: str = "running") -> int:
        """Atomically allocate a tasknum and insert a placeholder experiment."""
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(tasknum), -1) + 1 AS next FROM experiments"
            ).fetchone()
            tasknum = row["next"]
            self.conn.execute(
                """INSERT INTO experiments
                   (tasknum, approach, status)
                   VALUES (?, ?, ?)""",
                (tasknum, approach, status),
            )
            self.conn.commit()
            return tasknum

    def insert_experiment(self, exp: Experiment) -> int:
        with self._lock:
            self.conn.execute(
                """INSERT INTO experiments
                   (tasknum, parent_tasknum, approach, results, score, status,
                    stdout, stderr, diff, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (exp.tasknum, exp.parent_tasknum, exp.approach, exp.results,
                 exp.score, exp.status, exp.stdout, exp.stderr, exp.diff,
                 json.dumps(exp.metadata) if exp.metadata else None),
            )
            self.conn.commit()
            return exp.tasknum

    def update_experiment(self, tasknum: int, **kwargs):
        """Update specific fields of an experiment row."""
        allowed = {
            "approach", "results", "score", "status", "stdout", "stderr", "diff", "metadata"
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        if "metadata" in updates and isinstance(updates["metadata"], dict):
            updates["metadata"] = json.dumps(updates["metadata"])
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [tasknum]
        with self._lock:
            self.conn.execute(
                f"UPDATE experiments SET {set_clause} WHERE tasknum = ?", values
            )
            self.conn.commit()

    def get_experiment(self, tasknum: int) -> Optional[Experiment]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM experiments WHERE tasknum = ?", (tasknum,)
            ).fetchone()
        if not row:
            return None
        return self._row_to_experiment(row)

    def get_recent(self, limit: int = 20) -> list[Experiment]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM experiments ORDER BY tasknum DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_experiment(r) for r in rows]

    def get_best(self, n: int = 5) -> list[Experiment]:
        """Top N experiments by score (lowest = best). Only scored successes."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM experiments
                   WHERE score IS NOT NULL AND status IN ('success', 'judged')
                   ORDER BY score ASC LIMIT ?""",
                (n,),
            ).fetchall()
        return [self._row_to_experiment(r) for r in rows]

    def get_worst(self, n: int = 5) -> list[Experiment]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM experiments
                   WHERE score IS NOT NULL AND status IN ('success', 'judged')
                   ORDER BY score DESC LIMIT ?""",
                (n,),
            ).fetchall()
        return [self._row_to_experiment(r) for r in rows]

    def get_best_score(self) -> Optional[float]:
        with self._lock:
            row = self.conn.execute(
                """SELECT MIN(score) AS best FROM experiments
                   WHERE score IS NOT NULL AND status IN ('success', 'judged')"""
            ).fetchone()
        return row["best"] if row else None

    def count(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM experiments").fetchone()
        return row["n"]

    def get_all(self) -> list[Experiment]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM experiments ORDER BY tasknum ASC"
            ).fetchall()
        return [self._row_to_experiment(r) for r in rows]

    def _row_to_experiment(self, row: sqlite3.Row) -> Experiment:
        meta = row["metadata"]
        if meta:
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        else:
            meta = {}
        return Experiment(
            tasknum=row["tasknum"],
            parent_tasknum=row["parent_tasknum"],
            created_at=row["created_at"],
            approach=row["approach"],
            results=row["results"],
            score=row["score"],
            status=row["status"],
            stdout=row["stdout"],
            stderr=row["stderr"],
            diff=row["diff"],
            metadata=meta,
        )

    # ── Director Log ────────────────────────────────────────

    def save_director_entry(self, entry: DirectorEntry) -> int:
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO director_log (after_tasknum, summary, patterns_json)
                   VALUES (?, ?, ?)""",
                (entry.after_tasknum, entry.summary,
                 json.dumps(entry.patterns) if entry.patterns else None),
            )
            self.conn.commit()
            return cur.lastrowid

    def get_latest_director_entry(self) -> Optional[DirectorEntry]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM director_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        patterns = row["patterns_json"]
        if patterns:
            try:
                patterns = json.loads(patterns)
            except (json.JSONDecodeError, TypeError):
                patterns = {}
        else:
            patterns = {}
        return DirectorEntry(
            id=row["id"],
            created_at=row["created_at"],
            after_tasknum=row["after_tasknum"],
            summary=row["summary"],
            patterns=patterns,
        )

    # ── Utilities ───────────────────────────────────────────

    def close(self):
        self.conn.close()

    def print_summary(self):
        """Print a human-readable summary of all experiments."""
        exps = self.get_all()
        if not exps:
            print("No experiments yet.")
            return
        best = self.get_best_score()
        print(f"\n{'='*72}")
        print(f"  Experiments: {len(exps)}  |  Best score: {best}")
        print(f"{'='*72}")
        print(f"  {'#':>4}  {'Score':>8}  {'Status':<8}  Approach")
        print(f"  {'-'*4}  {'-'*8}  {'-'*8}  {'-'*44}")
        for e in exps:
            score_str = f"{e.score:.4f}" if e.score is not None else "   —"
            approach = (e.approach[:44] + "…") if len(e.approach) > 44 else e.approach
            marker = " ★" if e.score is not None and best is not None and e.score == best else ""
            print(f"  {e.tasknum:>4}  {score_str:>8}  {e.status:<8}  {approach}{marker}")
        print()
