"""Tests for the SQLite job-state layer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ifta.web import db
from ifta.web.models import SubmissionStatus


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


def test_init_db_creates_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    table_names = {r[0] for r in rows}
    assert "submissions" in table_names


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    db.init_db(path)
    db.init_db(path)  # second call must not raise


def test_create_submission_persists(db_path: Path) -> None:
    sub = db.create_submission(
        db_path,
        submission_id="abc123",
        email="customer@example.com",
        quarter="Q1-2026",
        confirm_token="tok-xyz",
        company="ABC Trucking",
    )
    assert sub.id == "abc123"
    assert sub.status == SubmissionStatus.PENDING_APPROVAL
    assert sub.company == "ABC Trucking"

    fetched = db.get_submission(db_path, "abc123")
    assert fetched is not None
    assert fetched.email == "customer@example.com"
    assert fetched.confirm_token == "tok-xyz"
    assert fetched.quarter == "Q1-2026"


def test_create_submission_status_override(db_path: Path) -> None:
    sub = db.create_submission(
        db_path,
        submission_id="pending1",
        email="a@b.co",
        quarter="Q1-2026",
        confirm_token="tok1",
        status=SubmissionStatus.PENDING_CONFIRMATION,
    )
    assert sub.status == SubmissionStatus.PENDING_CONFIRMATION
    fetched = db.get_submission(db_path, "pending1")
    assert fetched is not None
    assert fetched.status == SubmissionStatus.PENDING_CONFIRMATION


def test_get_submission_by_token(db_path: Path) -> None:
    db.create_submission(
        db_path,
        submission_id="xyz",
        email="a@b.co",
        quarter="Q1-2026",
        confirm_token="my-token",
    )
    found = db.get_submission_by_token(db_path, "my-token")
    assert found is not None
    assert found.id == "xyz"

    missing = db.get_submission_by_token(db_path, "nonsense")
    assert missing is None


def test_get_submission_missing_returns_none(db_path: Path) -> None:
    assert db.get_submission(db_path, "no-such-id") is None


def test_confirm_token_unique(db_path: Path) -> None:
    db.create_submission(
        db_path,
        submission_id="a",
        email="a@b.co",
        quarter="Q1-2026",
        confirm_token="same-token",
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.create_submission(
            db_path,
            submission_id="b",
            email="c@d.co",
            quarter="Q1-2026",
            confirm_token="same-token",
        )


def test_approve_submission_flips_pending_to_queued(db_path: Path) -> None:
    db.create_submission(
        db_path,
        submission_id="ap1",
        email="a@b.co",
        quarter="Q1-2026",
        confirm_token="tok-ap1",
        company="BLA BLA Transportation",
        trucks=15,
    )
    updated = db.approve_submission(db_path, "ap1")
    assert updated is not None
    assert updated.status == SubmissionStatus.QUEUED
    assert updated.approved_at is not None
    assert updated.trucks == 15

    # Now eligible for the worker.
    claimed = db.claim_next_queued(db_path)
    assert claimed is not None
    assert claimed.id == "ap1"


def test_reject_submission_flips_pending_to_rejected(db_path: Path) -> None:
    db.create_submission(
        db_path,
        submission_id="rj1",
        email="a@b.co",
        quarter="Q1-2026",
        confirm_token="tok-rj1",
    )
    updated = db.reject_submission(db_path, "rj1")
    assert updated is not None
    assert updated.status == SubmissionStatus.REJECTED

    # Rejected rows are never claimed by the worker.
    assert db.claim_next_queued(db_path) is None


def test_approve_is_idempotent_and_safe_on_wrong_state(db_path: Path) -> None:
    db.create_submission(
        db_path,
        submission_id="ap2",
        email="a@b.co",
        quarter="Q1-2026",
        confirm_token="tok-ap2",
    )
    first = db.approve_submission(db_path, "ap2")
    assert first is not None and first.status == SubmissionStatus.QUEUED
    # Second approve must not re-flip or wipe state — returns row unchanged.
    second = db.approve_submission(db_path, "ap2")
    assert second is not None and second.status == SubmissionStatus.QUEUED
    # Rejecting an already-queued row is a no-op.
    rejected = db.reject_submission(db_path, "ap2")
    assert rejected is not None and rejected.status == SubmissionStatus.QUEUED


def test_approve_missing_returns_none(db_path: Path) -> None:
    assert db.approve_submission(db_path, "ghost") is None
    assert db.reject_submission(db_path, "ghost") is None


def test_set_trucks_persists(db_path: Path) -> None:
    db.create_submission(
        db_path,
        submission_id="t1",
        email="a@b.co",
        quarter="Q1-2026",
        confirm_token="tok-t1",
    )
    db.set_trucks(db_path, "t1", 7)
    fetched = db.get_submission(db_path, "t1")
    assert fetched is not None
    assert fetched.trucks == 7


def test_init_db_migrates_legacy_table(tmp_path: Path) -> None:
    """A pre-migration DB (no trucks/approved_at) upgrades in place."""
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE submissions ("
            " id TEXT PRIMARY KEY, email TEXT NOT NULL, quarter TEXT NOT NULL,"
            " status TEXT NOT NULL, confirm_token TEXT NOT NULL UNIQUE,"
            " company TEXT, error TEXT, created_at TEXT NOT NULL,"
            " confirmed_at TEXT, started_at TEXT, finished_at TEXT,"
            " packet_sent_at TEXT)"
        )
        conn.execute(
            "INSERT INTO submissions (id, email, quarter, status, confirm_token, created_at)"
            " VALUES ('old', 'a@b.co', 'Q1-2026', 'queued', 'tok-old', '2026-01-01T00:00:00')"
        )
    db.init_db(path)  # must add trucks + approved_at without losing the row
    fetched = db.get_submission(path, "old")
    assert fetched is not None
    assert fetched.trucks is None
    assert fetched.approved_at is None
