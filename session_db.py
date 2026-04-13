"""Session state persistence for resumable workflows."""

import hashlib
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class SessionRecord:
    id: str
    codebase_path: str
    task_description: str
    status: str = "active"  # active | hibernated | complete | crashed
    current_phase: str = "rubric"  # rubric | baseline | loop | done
    phase_data: dict = field(default_factory=dict)
    cli_args: dict = field(default_factory=dict)
    experiments_db_path: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class IdeaRecord:
    """Persisted idea in the queue."""
    id: int
    title: str
    description: str
    rationale: str = ""
    risk: str = "medium"
    dispatched_at: Optional[str] = None


class SessionDB:
    """Manages a session SQLite database at a given path.

    Thread-safe via threading.Lock (same pattern as db.py).
    Uses WAL journal mode for concurrent read access.
    """

    def __init__(self, db_path: Path):
        self._path = db_path
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS session (
                    id TEXT PRIMARY KEY,
                    codebase_path TEXT NOT NULL,
                    task_description TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    status TEXT DEFAULT 'active',
                    current_phase TEXT DEFAULT 'rubric',
                    phase_data TEXT,
                    cli_args TEXT,
                    experiments_db_path TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS quota_events (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES session(id),
                    occurred_at TEXT DEFAULT (datetime('now')),
                    error_text TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    retry_after_seconds INTEGER
                );

                CREATE TABLE IF NOT EXISTS idea_queue (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES session(id),
                    created_at TEXT DEFAULT (datetime('now')),
                    dispatched_at TEXT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    rationale TEXT DEFAULT '',
                    risk TEXT DEFAULT 'medium'
                );

                CREATE INDEX IF NOT EXISTS idx_quota_events_session_time
                    ON quota_events(session_id, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_idea_queue_pending
                    ON idea_queue(session_id, dispatched_at);
            """)

    # ── Session lifecycle ──────────────────────────────────────────

    def create_session(self, record: SessionRecord) -> str:
        """Insert a new session. Returns the session ID. Idempotent if ID already exists."""
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO session
                   (id, codebase_path, task_description, status, current_phase,
                    phase_data, cli_args, experiments_db_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (record.id, record.codebase_path, record.task_description,
                 record.status, record.current_phase,
                 json.dumps(record.phase_data), json.dumps(record.cli_args),
                 record.experiments_db_path),
            )
            self._conn.commit()
        return record.id

    def get_session(self, session_id: str) -> Optional[SessionRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM session WHERE id = ?", (session_id,)
            ).fetchone()
        if not row:
            return None
        return SessionRecord(
            id=row["id"],
            codebase_path=row["codebase_path"],
            task_description=row["task_description"],
            status=row["status"],
            current_phase=row["current_phase"],
            phase_data=json.loads(row["phase_data"]) if row["phase_data"] else {},
            cli_args=json.loads(row["cli_args"]) if row["cli_args"] else {},
            experiments_db_path=row["experiments_db_path"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def update_session(self, session_id: str, **kwargs) -> None:
        """Update specific fields on a session. Allowed: status, current_phase, phase_data, cli_args."""
        allowed = {"status", "current_phase", "phase_data", "cli_args"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        # Serialize dicts to JSON
        if "phase_data" in updates and isinstance(updates["phase_data"], dict):
            updates["phase_data"] = json.dumps(updates["phase_data"])
        if "cli_args" in updates and isinstance(updates["cli_args"], dict):
            updates["cli_args"] = json.dumps(updates["cli_args"])

        updates["updated_at"] = datetime.utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [session_id]
        with self._lock:
            self._conn.execute(
                f"UPDATE session SET {set_clause} WHERE id = ?", values
            )
            self._conn.commit()

    def set_phase(self, session_id: str, phase: str, phase_data: Optional[dict] = None) -> None:
        updates = {"current_phase": phase}
        if phase_data is not None:
            updates["phase_data"] = phase_data
        self.update_session(session_id, **updates)

    def mark_hibernated(self, session_id: str) -> None:
        self.update_session(session_id, status="hibernated")

    def mark_complete(self, session_id: str) -> None:
        self.update_session(session_id, status="complete")

    def mark_crashed(self, session_id: str) -> None:
        self.update_session(session_id, status="crashed")

    # ── Quota tracking ─────────────────────────────────────────────

    def record_quota_event(self, session_id: str, error_text: str,
                           provider: str, model: str,
                           retry_after_seconds: Optional[int] = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO quota_events
                   (session_id, error_text, provider, model, retry_after_seconds)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, error_text[:500], provider, model, retry_after_seconds),
            )
            self._conn.commit()

    def get_quota_events_since(self, session_id: str, since_iso: str) -> list[dict]:
        """Get quota events since a given ISO timestamp."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM quota_events
                   WHERE session_id = ? AND occurred_at >= ?
                   ORDER BY occurred_at""",
                (session_id, since_iso),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_quota_events_in_window(self, session_id: str, window_seconds: int) -> int:
        """Count quota events in the last window_seconds."""
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) as cnt FROM quota_events
                   WHERE session_id = ?
                   AND occurred_at >= datetime('now', ?)""",
                (session_id, f"-{window_seconds} seconds"),
            ).fetchone()
        return row["cnt"] if row else 0

    # ── Idea queue ─────────────────────────────────────────────────

    def enqueue_ideas(self, session_id: str, ideas: list) -> None:
        """Add ideas to the queue. Each idea should have title, description, rationale, risk attributes."""
        with self._lock:
            for idea in ideas:
                self._conn.execute(
                    """INSERT INTO idea_queue (session_id, title, description, rationale, risk)
                       VALUES (?, ?, ?, ?, ?)""",
                    (session_id, idea.title, idea.description,
                     getattr(idea, 'rationale', ''), getattr(idea, 'risk', 'medium')),
                )
            self._conn.commit()

    def get_pending_ideas(self, session_id: str, limit: int = 10) -> list[IdeaRecord]:
        """Get undispatched ideas from the queue."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM idea_queue
                   WHERE session_id = ? AND dispatched_at IS NULL
                   ORDER BY id LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        return [
            IdeaRecord(
                id=r["id"], title=r["title"], description=r["description"],
                rationale=r["rationale"] or "", risk=r["risk"] or "medium",
            )
            for r in rows
        ]

    def mark_idea_dispatched(self, idea_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE idea_queue SET dispatched_at = datetime('now') WHERE id = ?",
                (idea_id,),
            )
            self._conn.commit()

    def pending_idea_count(self, session_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) as cnt FROM idea_queue WHERE session_id = ? AND dispatched_at IS NULL",
                (session_id,),
            ).fetchone()
        return row["cnt"] if row else 0

    def close(self):
        self._conn.close()


# ── Module-level helpers ───────────────────────────────────────────────

def session_id_for(codebase_path: Path, task_description: str) -> str:
    """Stable deterministic session ID: sha256(abs_path + ':' + task)[:16]."""
    key = f"{Path(codebase_path).resolve()}:{task_description}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def sessions_dir(base: Optional[Path] = None) -> Path:
    """Returns the sessions directory, creating it if needed."""
    if base is None:
        from .config import SESSIONS_DIR
        base = SESSIONS_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base


def session_db_path(session_id: str, base: Optional[Path] = None) -> Path:
    """Returns the path to a session's DB file."""
    return sessions_dir(base) / f"{session_id}.db"


def open_or_create_session(
    session_id: str,
    codebase_path: Path,
    task_description: str,
    cli_args: dict,
    experiments_db_path: Path,
    sessions_base: Optional[Path] = None,
) -> tuple:
    """
    Idempotent: opens existing session if found, creates new one otherwise.
    If existing session is 'hibernated', transitions it back to 'active'.
    Returns (SessionDB, SessionRecord).
    """
    db_path = session_db_path(session_id, sessions_base)
    sdb = SessionDB(db_path)

    existing = sdb.get_session(session_id)
    if existing:
        if existing.status == "hibernated":
            log.info(f"Reactivating hibernated session {session_id[:8]}...")
            sdb.update_session(session_id, status="active")
            existing.status = "active"
        return sdb, existing

    record = SessionRecord(
        id=session_id,
        codebase_path=str(Path(codebase_path).resolve()),
        task_description=task_description,
        cli_args=cli_args,
        experiments_db_path=str(Path(experiments_db_path).resolve()),
    )
    sdb.create_session(record)
    return sdb, record


def find_session_by_codebase(codebase_path: Path, sessions_base: Optional[Path] = None) -> Optional[tuple]:
    """
    Scan sessions directory for the most recent session matching this codebase.
    Returns (session_id, SessionDB) or None.
    """
    sdir = sessions_dir(sessions_base)
    abs_codebase = str(Path(codebase_path).resolve())

    best = None  # (updated_at, session_id, db_path)
    for db_file in sdir.glob("*.db"):
        try:
            conn = sqlite3.connect(str(db_file))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, codebase_path, updated_at FROM session WHERE codebase_path = ? ORDER BY updated_at DESC LIMIT 1",
                (abs_codebase,),
            ).fetchone()
            conn.close()
            if row:
                updated = row["updated_at"] or ""
                if best is None or updated > best[0]:
                    best = (updated, row["id"], db_file)
        except Exception:
            continue

    if best is None:
        return None

    sdb = SessionDB(best[2])
    return best[1], sdb
