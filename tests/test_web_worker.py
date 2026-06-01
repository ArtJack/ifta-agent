"""Tests for the polling worker.

Worker tests don't need real pipeline runs — they exercise the state machine
and callbacks. A separate test_web_pipeline.py covers the actual pipeline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ifta.web import db, worker
from ifta.web.models import Submission, SubmissionStatus
from ifta.web.pipeline import PipelineError


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


@pytest.fixture
def submissions_dir(tmp_path: Path) -> Path:
    d = tmp_path / "subs"
    d.mkdir()
    return d


def _create_queued(db_path: Path, sid: str = "s1", quarter: str = "Q1-2026") -> Submission:
    return db.create_submission(
        db_path,
        submission_id=sid,
        email="customer@example.com",
        quarter=quarter,
        confirm_token=f"tok-{sid}",
    )


def test_process_one_job_empty_queue(db_path: Path, submissions_dir: Path) -> None:
    assert worker.process_one_job(db_path, submissions_dir) is None


def test_process_one_job_success_path(
    db_path: Path, submissions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub_in = _create_queued(db_path)

    def fake_process(_subs_dir: Path, sub: Submission) -> Path:
        out = submissions_dir / sub.id / "outputs" / sub.quarter
        out.mkdir(parents=True)
        (out / "ifta_portal.csv").write_text("ok")
        return out

    monkeypatch.setattr(worker, "process_submission", fake_process)

    successes: list[tuple[str, Path]] = []
    sub_out = worker.process_one_job(
        db_path,
        submissions_dir,
        on_success=lambda s, p: successes.append((s.id, p)),
    )
    assert sub_out is not None
    assert sub_out.status == SubmissionStatus.DONE
    assert sub_out.finished_at is not None
    assert sub_out.error is None
    assert successes == [(sub_in.id, submissions_dir / sub_in.id / "outputs" / sub_in.quarter)]


def test_process_one_job_pipeline_error_path(
    db_path: Path, submissions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_queued(db_path, "bad")

    def fake_process(_subs_dir: Path, sub: Submission) -> Path:
        raise PipelineError("bad uploads")

    monkeypatch.setattr(worker, "process_submission", fake_process)

    failures: list[tuple[str, str]] = []
    sub_out = worker.process_one_job(
        db_path,
        submissions_dir,
        on_failure=lambda s, msg: failures.append((s.id, msg)),
    )
    assert sub_out is not None
    assert sub_out.status == SubmissionStatus.FAILED
    assert sub_out.error == "bad uploads"
    assert failures == [("bad", "bad uploads")]


def test_process_one_job_unexpected_error_marks_failed(
    db_path: Path, submissions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_queued(db_path, "boom")

    def fake_process(_subs_dir: Path, _sub: Submission) -> Path:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(worker, "process_submission", fake_process)
    sub_out = worker.process_one_job(db_path, submissions_dir)
    assert sub_out is not None
    assert sub_out.status == SubmissionStatus.FAILED
    assert sub_out.error is not None
    assert "kaboom" in sub_out.error


def test_claim_next_queued_marks_running(db_path: Path) -> None:
    sub = _create_queued(db_path, "claim_me")
    claimed = db.claim_next_queued(db_path)
    assert claimed is not None
    assert claimed.id == sub.id
    assert claimed.status == SubmissionStatus.RUNNING
    assert claimed.started_at is not None
    # Second claim returns None (already running, not queued anymore).
    assert db.claim_next_queued(db_path) is None


def test_claim_next_queued_oldest_first(db_path: Path) -> None:
    import time

    _create_queued(db_path, "first")
    time.sleep(0.01)  # ensure distinct created_at
    _create_queued(db_path, "second")
    claimed = db.claim_next_queued(db_path)
    assert claimed is not None
    assert claimed.id == "first"


def test_list_submissions_filters_by_status(db_path: Path) -> None:
    _create_queued(db_path, "q1")
    db.claim_next_queued(db_path)  # → RUNNING
    _create_queued(db_path, "q2")
    queued = db.list_submissions(db_path, status=SubmissionStatus.QUEUED)
    running = db.list_submissions(db_path, status=SubmissionStatus.RUNNING)
    all_ = db.list_submissions(db_path)
    assert [s.id for s in queued] == ["q2"]
    assert [s.id for s in running] == ["q1"]
    assert {s.id for s in all_} == {"q1", "q2"}


def test_mark_done_clears_error(db_path: Path) -> None:
    sub = _create_queued(db_path, "recovered")
    db.claim_next_queued(db_path)
    db.mark_failed(db_path, sub.id, error="transient")
    db.mark_done(db_path, sub.id)
    fetched = db.get_submission(db_path, sub.id)
    assert fetched is not None
    assert fetched.status == SubmissionStatus.DONE
    assert fetched.error is None


def test_mark_done_rejects_pending_approval(db_path: Path) -> None:
    # BUG-007: a job awaiting operator approval must not jump straight to DONE.
    db.create_submission(
        db_path,
        submission_id="pa",
        email="customer@example.com",
        quarter="Q1-2026",
        confirm_token="tok-pa",
        status=SubmissionStatus.PENDING_APPROVAL,
    )
    db.mark_done(db_path, "pa")
    fetched = db.get_submission(db_path, "pa")
    assert fetched is not None
    assert fetched.status == SubmissionStatus.PENDING_APPROVAL  # unchanged


def test_mark_failed_rejects_done_to_failed(db_path: Path) -> None:
    # BUG-007: a completed job must not be re-marked failed.
    _create_queued(db_path, "fin")
    db.claim_next_queued(db_path)  # -> RUNNING
    db.mark_done(db_path, "fin")  # -> DONE
    db.mark_failed(db_path, "fin", error="too late")
    fetched = db.get_submission(db_path, "fin")
    assert fetched is not None
    assert fetched.status == SubmissionStatus.DONE  # unchanged
    assert fetched.error is None  # not overwritten


def test_reap_stale_running_flips_to_failed(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows stuck in RUNNING past the cutoff get marked FAILED (bug_004)."""
    import sqlite3
    from datetime import UTC, datetime, timedelta

    sub = _create_queued(db_path, "stuck")
    db.claim_next_queued(db_path)  # flips to RUNNING, sets started_at=now
    # Backdate started_at to 1 hour ago so it's past any reasonable cutoff.
    long_ago = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE submissions SET started_at = ? WHERE id = ?",
            (long_ago, sub.id),
        )
        conn.commit()

    reaped = db.reap_stale_running(db_path, max_seconds_running=900)
    assert [r.id for r in reaped] == [sub.id]
    fetched = db.get_submission(db_path, sub.id)
    assert fetched is not None
    assert fetched.status.value == "failed"
    assert fetched.error is not None
    assert "worker stopped" in fetched.error.lower()


def test_reap_stale_running_leaves_recent_rows_alone(db_path: Path) -> None:
    """A worker currently mid-job should not be killed by a startup reap."""
    sub = _create_queued(db_path, "fresh")
    db.claim_next_queued(db_path)  # started_at = now
    reaped = db.reap_stale_running(db_path, max_seconds_running=900)
    assert reaped == []
    fetched = db.get_submission(db_path, sub.id)
    assert fetched is not None
    assert fetched.status.value == "running"


def test_run_forever_reaps_on_startup(
    db_path: Path,
    submissions_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_forever calls the reaper before entering the loop, firing
    on_failure callbacks for orphans (bug_004 + merged_bug_009 surfacing)."""
    import sqlite3
    from datetime import UTC, datetime, timedelta

    _create_queued(db_path, "ghost")
    db.claim_next_queued(db_path)
    long_ago = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE submissions SET started_at = ? WHERE id = ?",
            (long_ago, "ghost"),
        )
        conn.commit()

    failures: list[tuple[str, str]] = []

    # Patch the polling loop so we don't block forever — make process_one_job
    # raise KeyboardInterrupt immediately so run_forever returns after the
    # reap step.
    def fake_process_one_job(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(worker, "process_one_job", fake_process_one_job)

    worker.run_forever(
        db_path,
        submissions_dir,
        on_failure=lambda s, msg: failures.append((s.id, msg)),
    )
    assert len(failures) == 1
    assert failures[0][0] == "ghost"
    fetched = db.get_submission(db_path, "ghost")
    assert fetched is not None
    assert fetched.status.value == "failed"


def test_mark_failed_truncates_long_errors(db_path: Path) -> None:
    sub = _create_queued(db_path, "huge_err")
    db.claim_next_queued(db_path)
    huge = "x" * 10000
    db.mark_failed(db_path, sub.id, error=huge)
    fetched = db.get_submission(db_path, sub.id)
    assert fetched is not None
    assert fetched.error is not None
    assert len(fetched.error) <= 4000
