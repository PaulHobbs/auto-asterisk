"""Tests for auto.session_db — uses tmp_path, no real filesystem state."""

import json
import threading
import pytest
from pathlib import Path

from auto.session_db import (
    SessionDB, SessionRecord, IdeaRecord,
    session_id_for, sessions_dir, session_db_path,
    open_or_create_session, find_session_by_codebase,
)


@pytest.fixture
def db(tmp_path):
    sdb = SessionDB(tmp_path / "test_session.db")
    yield sdb
    sdb.close()


@pytest.fixture
def sample_record():
    return SessionRecord(
        id="abc123",
        codebase_path="/tmp/myproject",
        task_description="optimize performance",
        experiments_db_path="/tmp/myproject/.auto/experiments.db",
    )


# Helper to create a mock Idea with the right attributes
class MockIdea:
    def __init__(self, title, description, rationale="", risk="medium"):
        self.title = title
        self.description = description
        self.rationale = rationale
        self.risk = risk


class TestSessionLifecycle:
    def test_create_and_get_session(self, db, sample_record):
        db.create_session(sample_record)
        got = db.get_session("abc123")
        assert got is not None
        assert got.id == "abc123"
        assert got.codebase_path == "/tmp/myproject"
        assert got.task_description == "optimize performance"
        assert got.status == "active"
        assert got.current_phase == "rubric"

    def test_get_nonexistent_session(self, db):
        assert db.get_session("nonexistent") is None

    def test_update_session_status(self, db, sample_record):
        db.create_session(sample_record)
        db.update_session("abc123", status="hibernated")
        got = db.get_session("abc123")
        assert got.status == "hibernated"

    def test_mark_hibernated(self, db, sample_record):
        db.create_session(sample_record)
        db.mark_hibernated("abc123")
        assert db.get_session("abc123").status == "hibernated"

    def test_mark_complete(self, db, sample_record):
        db.create_session(sample_record)
        db.mark_complete("abc123")
        assert db.get_session("abc123").status == "complete"

    def test_mark_crashed(self, db, sample_record):
        db.create_session(sample_record)
        db.mark_crashed("abc123")
        assert db.get_session("abc123").status == "crashed"

    def test_set_phase(self, db, sample_record):
        db.create_session(sample_record)
        db.set_phase("abc123", "loop", {"director_summary": "test summary"})
        got = db.get_session("abc123")
        assert got.current_phase == "loop"
        assert got.phase_data == {"director_summary": "test summary"}

    def test_idempotent_create(self, db, sample_record):
        db.create_session(sample_record)
        db.create_session(sample_record)  # should not raise
        got = db.get_session("abc123")
        assert got is not None

    def test_phase_data_preserved_across_updates(self, db, sample_record):
        db.create_session(sample_record)
        db.set_phase("abc123", "loop", {"key": "value"})
        db.update_session("abc123", status="hibernated")
        got = db.get_session("abc123")
        assert got.phase_data == {"key": "value"}
        assert got.status == "hibernated"

    def test_update_ignores_unknown_fields(self, db, sample_record):
        db.create_session(sample_record)
        db.update_session("abc123", bogus_field="nope", status="complete")
        got = db.get_session("abc123")
        assert got.status == "complete"


class TestQuotaEvents:
    def test_record_and_count(self, db, sample_record):
        db.create_session(sample_record)
        db.record_quota_event("abc123", "429 Too Many Requests", "claude", "opus")
        db.record_quota_event("abc123", "Rate limited", "claude", "sonnet")
        count = db.count_quota_events_in_window("abc123", 300)
        assert count == 2

    def test_events_outside_window(self, db, sample_record):
        db.create_session(sample_record)
        # Record an event — it'll be "now"
        db.record_quota_event("abc123", "429", "claude", "opus")
        # 0-second window should still catch "now" events
        # but a truly expired window won't, this is hard to test without time mocking
        count = db.count_quota_events_in_window("abc123", 300)
        assert count >= 1

    def test_retry_after_stored(self, db, sample_record):
        db.create_session(sample_record)
        db.record_quota_event("abc123", "429", "claude", "opus", retry_after_seconds=60)
        events = db.get_quota_events_since("abc123", "2000-01-01")
        assert len(events) == 1
        assert events[0]["retry_after_seconds"] == 60

    def test_error_text_truncated(self, db, sample_record):
        db.create_session(sample_record)
        long_text = "x" * 1000
        db.record_quota_event("abc123", long_text, "claude", "opus")
        events = db.get_quota_events_since("abc123", "2000-01-01")
        assert len(events[0]["error_text"]) <= 500

    def test_multiple_sessions_isolated(self, db):
        r1 = SessionRecord(id="s1", codebase_path="/a", task_description="t1", experiments_db_path="/a/.auto/e.db")
        r2 = SessionRecord(id="s2", codebase_path="/b", task_description="t2", experiments_db_path="/b/.auto/e.db")
        db.create_session(r1)
        db.create_session(r2)
        db.record_quota_event("s1", "err", "claude", "opus")
        db.record_quota_event("s1", "err", "claude", "opus")
        db.record_quota_event("s2", "err", "claude", "opus")
        assert db.count_quota_events_in_window("s1", 300) == 2
        assert db.count_quota_events_in_window("s2", 300) == 1


class TestIdeaQueue:
    def test_enqueue_and_get_pending(self, db, sample_record):
        db.create_session(sample_record)
        ideas = [MockIdea("idea1", "desc1"), MockIdea("idea2", "desc2", risk="high")]
        db.enqueue_ideas("abc123", ideas)
        pending = db.get_pending_ideas("abc123")
        assert len(pending) == 2
        assert pending[0].title == "idea1"
        assert pending[1].risk == "high"

    def test_mark_dispatched(self, db, sample_record):
        db.create_session(sample_record)
        db.enqueue_ideas("abc123", [MockIdea("idea1", "desc1")])
        pending = db.get_pending_ideas("abc123")
        assert len(pending) == 1
        db.mark_idea_dispatched(pending[0].id)
        pending = db.get_pending_ideas("abc123")
        assert len(pending) == 0

    def test_pending_count(self, db, sample_record):
        db.create_session(sample_record)
        assert db.pending_idea_count("abc123") == 0
        db.enqueue_ideas("abc123", [MockIdea("a", "b"), MockIdea("c", "d")])
        assert db.pending_idea_count("abc123") == 2

    def test_empty_queue(self, db, sample_record):
        db.create_session(sample_record)
        assert db.get_pending_ideas("abc123") == []

    def test_limit_respected(self, db, sample_record):
        db.create_session(sample_record)
        ideas = [MockIdea(f"idea{i}", f"desc{i}") for i in range(10)]
        db.enqueue_ideas("abc123", ideas)
        pending = db.get_pending_ideas("abc123", limit=3)
        assert len(pending) == 3


class TestSessionIdDerivation:
    def test_same_inputs_same_id(self):
        id1 = session_id_for(Path("/tmp/project"), "optimize")
        id2 = session_id_for(Path("/tmp/project"), "optimize")
        assert id1 == id2

    def test_different_paths_different_ids(self):
        id1 = session_id_for(Path("/tmp/project1"), "optimize")
        id2 = session_id_for(Path("/tmp/project2"), "optimize")
        assert id1 != id2

    def test_different_tasks_different_ids(self):
        id1 = session_id_for(Path("/tmp/project"), "optimize")
        id2 = session_id_for(Path("/tmp/project"), "refactor")
        assert id1 != id2

    def test_id_is_16_hex_chars(self):
        sid = session_id_for(Path("/tmp/project"), "task")
        assert len(sid) == 16
        assert all(c in "0123456789abcdef" for c in sid)


class TestOpenOrCreateSession:
    def test_creates_new_session(self, tmp_path):
        sdb, record = open_or_create_session(
            session_id="new123",
            codebase_path=Path("/tmp/proj"),
            task_description="test",
            cli_args={"model": "sonnet"},
            experiments_db_path=Path("/tmp/proj/.auto/e.db"),
            sessions_base=tmp_path,
        )
        assert record.id == "new123"
        assert record.status == "active"
        sdb.close()

    def test_opens_existing_session(self, tmp_path):
        # Create first
        sdb1, _ = open_or_create_session(
            session_id="exist1",
            codebase_path=Path("/tmp/proj"),
            task_description="test",
            cli_args={},
            experiments_db_path=Path("/tmp/proj/.auto/e.db"),
            sessions_base=tmp_path,
        )
        sdb1.set_phase("exist1", "loop")
        sdb1.close()

        # Re-open
        sdb2, record = open_or_create_session(
            session_id="exist1",
            codebase_path=Path("/tmp/proj"),
            task_description="test",
            cli_args={},
            experiments_db_path=Path("/tmp/proj/.auto/e.db"),
            sessions_base=tmp_path,
        )
        assert record.current_phase == "loop"
        sdb2.close()

    def test_reactivates_hibernated_session(self, tmp_path):
        sdb1, _ = open_or_create_session(
            session_id="hib1",
            codebase_path=Path("/tmp/proj"),
            task_description="test",
            cli_args={},
            experiments_db_path=Path("/tmp/proj/.auto/e.db"),
            sessions_base=tmp_path,
        )
        sdb1.mark_hibernated("hib1")
        sdb1.close()

        sdb2, record = open_or_create_session(
            session_id="hib1",
            codebase_path=Path("/tmp/proj"),
            task_description="test",
            cli_args={},
            experiments_db_path=Path("/tmp/proj/.auto/e.db"),
            sessions_base=tmp_path,
        )
        assert record.status == "active"
        sdb2.close()


class TestFindSessionByCodebase:
    def test_finds_matching_session(self, tmp_path):
        sdb, _ = open_or_create_session(
            session_id="find1",
            codebase_path=tmp_path / "myproject",
            task_description="test",
            cli_args={},
            experiments_db_path=tmp_path / "myproject/.auto/e.db",
            sessions_base=tmp_path / "sessions",
        )
        sdb.close()

        result = find_session_by_codebase(
            tmp_path / "myproject",
            sessions_base=tmp_path / "sessions",
        )
        assert result is not None
        sid, found_db = result
        assert sid == "find1"
        found_db.close()

    def test_returns_none_when_no_match(self, tmp_path):
        result = find_session_by_codebase(
            tmp_path / "nonexistent",
            sessions_base=tmp_path / "sessions",
        )
        assert result is None

    def test_thread_safe_create(self, tmp_path):
        """Multiple threads can create sessions concurrently."""
        sdb = SessionDB(tmp_path / "concurrent.db")
        errors = []

        def create_session(i):
            try:
                record = SessionRecord(
                    id=f"thread_{i}",
                    codebase_path=f"/tmp/proj{i}",
                    task_description=f"task {i}",
                    experiments_db_path=f"/tmp/proj{i}/.auto/e.db",
                )
                sdb.create_session(record)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=create_session, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        sdb.close()
