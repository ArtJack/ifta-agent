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
    assert sub.status == SubmissionStatus.QUEUED
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
