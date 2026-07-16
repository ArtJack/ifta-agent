"""Postgres backend + backend-dispatch tests.

Hermetic by default: the dispatch tests need no database, and the round-trip
tests skip unless IFTA_TEST_PG_URL points at a scratch Postgres. To run the
full set locally:

    docker run --rm -e POSTGRES_PASSWORD=pw -p 5432:5432 postgres:16
    IFTA_TEST_PG_URL='postgresql://postgres:pw@localhost:5432/postgres' \
        .venv/bin/pytest tests/test_web_db_postgres.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ifta.web import db
from ifta.web.models import SubmissionStatus

# Postgres ignores the path arg (it uses IFTA_WEB_DB_URL); a sentinel documents that.
UNUSED = Path("unused-by-postgres")


def test_backend_defaults_to_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IFTA_WEB_DB_URL", raising=False)
    assert db._backend().__name__ == "ifta.web.db_sqlite"


def test_backend_selects_postgres_when_url_set(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("psycopg")
    monkeypatch.setenv("IFTA_WEB_DB_URL", "postgresql://u:p@localhost:5432/ifta")
    assert db._backend().__name__ == "ifta.web.db_postgres"


@pytest.fixture
def pg(monkeypatch: pytest.MonkeyPatch):
    """Real Postgres backend against IFTA_TEST_PG_URL; skipped when unset."""
    pytest.importorskip("psycopg")
    url = os.environ.get("IFTA_TEST_PG_URL")
    if not url:
        pytest.skip("set IFTA_TEST_PG_URL to a scratch Postgres DSN to run these")
    import psycopg

    monkeypatch.setenv("IFTA_WEB_DB_URL", url)
    db.init_db(UNUSED)
    with psycopg.connect(url) as conn:  # start each test from a clean table
        conn.execute("TRUNCATE submissions")
        conn.commit()
    return db


def test_postgres_submission_lifecycle(pg) -> None:
    sub = pg.create_submission(
        UNUSED,
        submission_id="pg-1",
        email="carrier@example.com",
        quarter="Q1-2026",
        confirm_token="tok-pg-1",
    )
    assert sub.status is SubmissionStatus.QUEUED

    fetched = pg.get_submission(UNUSED, "pg-1")
    assert fetched is not None and fetched.email == "carrier@example.com"

    claimed = pg.claim_next_queued(UNUSED)
    assert claimed is not None and claimed.id == "pg-1"
    assert claimed.status is SubmissionStatus.RUNNING

    # The row is now RUNNING, so a second claim finds nothing queued.
    assert pg.claim_next_queued(UNUSED) is None

    pg.mark_done(UNUSED, "pg-1")
    done = pg.get_submission(UNUSED, "pg-1")
    assert done is not None and done.status is SubmissionStatus.DONE


def test_postgres_claim_is_oldest_first(pg) -> None:
    for i in range(3):
        pg.create_submission(
            UNUSED,
            submission_id=f"pg-{i}",
            email="c@example.com",
            quarter="Q1-2026",
            confirm_token=f"tok-{i}",
        )
    first = pg.claim_next_queued(UNUSED)
    second = pg.claim_next_queued(UNUSED)
    assert first is not None and first.id == "pg-0"
    assert second is not None and second.id == "pg-1"


def test_postgres_mark_failed_then_done_recovers(pg) -> None:
    pg.create_submission(
        UNUSED,
        submission_id="pg-x",
        email="c@example.com",
        quarter="Q1-2026",
        confirm_token="tok-x",
    )
    pg.claim_next_queued(UNUSED)
    pg.mark_failed(UNUSED, "pg-x", error="boom")
    failed = pg.get_submission(UNUSED, "pg-x")
    assert failed is not None and failed.status is SubmissionStatus.FAILED

    # A FAILED row may be recovered to DONE (matches the SQLite guard).
    pg.mark_done(UNUSED, "pg-x")
    done = pg.get_submission(UNUSED, "pg-x")
    assert done is not None and done.status is SubmissionStatus.DONE
