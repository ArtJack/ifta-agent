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


def test_mark_failed_truncates_long_errors(db_path: Path) -> None:
    sub = _create_queued(db_path, "huge_err")
    db.claim_next_queued(db_path)
    huge = "x" * 10000
    db.mark_failed(db_path, sub.id, error=huge)
    fetched = db.get_submission(db_path, sub.id)
    assert fetched is not None
    assert fetched.error is not None
    assert len(fetched.error) <= 4000
