"""Hypothesis property tests for auto.session_db."""

import json
import tempfile
import threading
import itertools
from pathlib import Path
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from auto.session_db import (
    SessionDB, SessionRecord, IdeaRecord,
    session_id_for, open_or_create_session,
)

# ── Strategies ─────────────────────────────────────────────────────────

safe_text = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
    min_size=1, max_size=100,
)

status_st = st.sampled_from(["active", "hibernated", "complete", "crashed"])
phase_st = st.sampled_from(["rubric", "baseline", "loop", "done"])

phase_data_st = st.dictionaries(
    st.text(alphabet=st.characters(whitelist_categories=("Ll",)), min_size=1, max_size=20),
    st.one_of(st.text(max_size=50), st.integers(-1000, 1000), st.floats(allow_nan=False, allow_infinity=False)),
    max_size=5,
)

# Thread-safe counter to generate unique session IDs across Hypothesis examples.
_counter = itertools.count()


def _fresh_db():
    """Return (SessionDB, tmp_dir_path) backed by a unique temp directory."""
    tmp = Path(tempfile.mkdtemp())
    db = SessionDB(tmp / "test.db")
    return db, tmp


# ── Session state machine ─────────────────────────────────────────────

@given(transitions=st.lists(status_st, min_size=1, max_size=10))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_session_accepts_any_status_transitions(transitions):
    """SessionDB never raises on any sequence of status updates."""
    db, _ = _fresh_db()
    sid = f"sess-{next(_counter)}"
    record = SessionRecord(
        id=sid, codebase_path="/tmp", task_description="t",
        experiments_db_path="/tmp/e.db",
    )
    db.create_session(record)
    for status in transitions:
        db.update_session(sid, status=status)
    got = db.get_session(sid)
    assert got.status == transitions[-1]
    db.close()


@given(phase=phase_st, data=phase_data_st)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_phase_data_roundtrips(phase, data):
    """set_phase + get_session always round-trips phase_data correctly."""
    db, _ = _fresh_db()
    sid = f"rt-{next(_counter)}"
    record = SessionRecord(
        id=sid, codebase_path="/tmp", task_description="t",
        experiments_db_path="/tmp/e.db",
    )
    db.create_session(record)
    db.set_phase(sid, phase, data)
    got = db.get_session(sid)
    assert got.current_phase == phase
    # JSON roundtrip may change float precision, compare via JSON
    assert json.loads(json.dumps(got.phase_data)) == json.loads(json.dumps(data))
    db.close()


# ── Idea queue ─────────────────────────────────────────────────────────

class MockIdea:
    def __init__(self, title, description, rationale="", risk="medium"):
        self.title = title
        self.description = description
        self.rationale = rationale
        self.risk = risk


@given(
    ideas=st.lists(
        st.fixed_dictionaries({
            "title": safe_text,
            "description": safe_text,
            "rationale": st.text(max_size=100),
            "risk": st.sampled_from(["low", "medium", "high"]),
        }),
        min_size=0, max_size=15,
    )
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_idea_queue_roundtrip(ideas):
    """All enqueued ideas appear in get_pending_ideas."""
    db, _ = _fresh_db()
    sid = f"iq-{next(_counter)}"
    record = SessionRecord(
        id=sid, codebase_path="/tmp", task_description="t",
        experiments_db_path="/tmp/e.db",
    )
    db.create_session(record)

    mock_ideas = [MockIdea(**i) for i in ideas]
    db.enqueue_ideas(sid, mock_ideas)

    pending = db.get_pending_ideas(sid, limit=100)
    assert len(pending) == len(ideas)
    for i, p in enumerate(pending):
        assert p.title == ideas[i]["title"]
        assert p.description == ideas[i]["description"]
    db.close()


# ── Concurrent quota event recording ──────────────────────────────────

@given(
    n_threads=st.integers(2, 8),
    events_per_thread=st.integers(1, 5),
)
@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_concurrent_quota_events_no_loss(n_threads, events_per_thread):
    """Total events recorded == n_threads * events_per_thread under concurrent writes."""
    db, _ = _fresh_db()
    sid = f"conc-{next(_counter)}"
    record = SessionRecord(
        id=sid, codebase_path="/tmp", task_description="t",
        experiments_db_path="/tmp/e.db",
    )
    db.create_session(record)

    errors = []

    def writer(thread_id):
        try:
            for j in range(events_per_thread):
                db.record_quota_event(
                    sid, f"error from thread {thread_id} event {j}",
                    "claude", "sonnet",
                )
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    count = db.count_quota_events_in_window(sid, 3600)
    assert count == n_threads * events_per_thread
    db.close()


# ── Session ID derivation ─────────────────────────────────────────────

@given(
    path=st.text(
        alphabet=st.characters(blacklist_characters="\x00", blacklist_categories=("Cs",)),
        min_size=1, max_size=100,
    ),
    task=st.text(min_size=1, max_size=200),
)
def test_session_id_is_deterministic(path, task):
    """Same inputs always produce the same session ID."""
    id1 = session_id_for(Path(path), task)
    id2 = session_id_for(Path(path), task)
    assert id1 == id2
    assert len(id1) == 16
    assert all(c in "0123456789abcdef" for c in id1)
