"""Unit tests for auto.db — queries, thread safety, edge cases."""

import tempfile
import threading
from pathlib import Path

import pytest
from auto.db import DB, Experiment, Rubric


@pytest.fixture
def db(tmp_path):
    d = DB(tmp_path / "test.db")
    yield d
    d.close()


class TestExperiments:
    def test_insert_and_get(self, db):
        exp = Experiment(tasknum=0, approach="test approach", status="running")
        db.insert_experiment(exp)
        got = db.get_experiment(0)
        assert got is not None
        assert got.approach == "test approach"
        assert got.status == "running"

    def test_next_tasknum(self, db):
        assert db.next_tasknum() == 0
        db.insert_experiment(Experiment(tasknum=0, approach="a"))
        assert db.next_tasknum() == 1

    def test_reserve_tasknum(self, db):
        t0 = db.reserve_tasknum("first")
        t1 = db.reserve_tasknum("second")
        assert t0 == 0
        assert t1 == 1
        assert db.get_experiment(0).approach == "first"
        assert db.get_experiment(1).approach == "second"

    def test_update_experiment(self, db):
        db.insert_experiment(Experiment(tasknum=0, approach="a", status="running"))
        db.update_experiment(0, score=1.5, status="judged")
        got = db.get_experiment(0)
        assert got.score == 1.5
        assert got.status == "judged"

    def test_update_approach(self, db):
        db.insert_experiment(Experiment(tasknum=0, approach="old", status="running"))
        db.update_experiment(0, approach="new")
        assert db.get_experiment(0).approach == "new"

    def test_best_score(self, db):
        assert db.get_best_score() is None
        db.insert_experiment(Experiment(tasknum=0, approach="a", score=10.0, status="judged"))
        db.insert_experiment(Experiment(tasknum=1, approach="b", score=5.0, status="judged"))
        db.insert_experiment(Experiment(tasknum=2, approach="c", score=15.0, status="judged"))
        assert db.get_best_score() == 5.0

    def test_best_and_worst(self, db):
        for i, score in enumerate([10.0, 5.0, 15.0, 2.0, 8.0]):
            db.insert_experiment(Experiment(tasknum=i, approach=f"exp{i}", score=score, status="judged"))
        best = db.get_best(n=2)
        assert [e.score for e in best] == [2.0, 5.0]
        worst = db.get_worst(n=2)
        assert [e.score for e in worst] == [15.0, 10.0]

    def test_count(self, db):
        assert db.count() == 0
        db.insert_experiment(Experiment(tasknum=0, approach="a"))
        assert db.count() == 1

    def test_metadata_roundtrip(self, db):
        db.insert_experiment(Experiment(tasknum=0, approach="a", metadata={"key": "val"}))
        got = db.get_experiment(0)
        assert got.metadata == {"key": "val"}

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


class TestRubric:
    def test_save_and_get(self, db):
        rubric = Rubric(
            task_description="test task",
            scoring_dimensions="dims",
            judge_prompt="judge",
            setup_code="echo hi",
        )
        rid = db.save_rubric(rubric)
        assert db.get_rubric() is None  # not yet approved

        db.approve_rubric(rid)
        got = db.get_rubric()
        assert got is not None
        assert got.task_description == "test task"
        assert got.setup_code == "echo hi"


class TestThreadSafety:
    def test_concurrent_reserve_tasknum(self, db):
        """Multiple threads reserving tasknums should get unique numbers."""
        results = []
        errors = []

        def worker():
            try:
                t = db.reserve_tasknum("concurrent")
                results.append(t)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(results) == 20
        assert len(set(results)) == 20  # all unique
