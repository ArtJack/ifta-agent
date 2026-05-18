"""SQLite-backed job state for web intake.

One connection per call — SQLite handles single-writer concurrency fine for
the volumes we expect (one customer at a time). WAL mode is enabled at init
so readers (status endpoint) never block the worker.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ifta.web.models import Submission, SubmissionStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    quarter TEXT NOT NULL,
    status TEXT NOT NULL,
    confirm_token TEXT NOT NULL UNIQUE,
    company TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    packet_sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON submissions(status);
CREATE INDEX IF NOT EXISTS idx_confirm_token ON submissions(confirm_token);
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def init_db(path: Path) -> None:
    """Create the schema (idempotent) and enable WAL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def create_submission(
    path: Path,
    *,
    submission_id: str,
    email: str,
    quarter: str,
    confirm_token: str,
    company: str | None = None,
    status: SubmissionStatus = SubmissionStatus.QUEUED,
) -> Submission:
    """Insert a new submission row.

    The default status is QUEUED so Phase 1 submissions are immediately
    eligible for the worker (Phase 2). Phase 3 will switch the default to
    PENDING_CONFIRMATION and add the email-confirmation flow.
    """
    created_at_iso = _now_iso()
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO submissions"
            " (id, email, quarter, status, confirm_token, company, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                submission_id,
                email,
                quarter,
                status.value,
                confirm_token,
                company,
                created_at_iso,
            ),
        )
    return Submission(
        id=submission_id,
        email=email,
        quarter=quarter,
        status=status,
        confirm_token=confirm_token,
        company=company,
        created_at=datetime.fromisoformat(created_at_iso),
    )


def get_submission(path: Path, submission_id: str) -> Submission | None:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
    return _row_to_submission(row) if row else None


def get_submission_by_token(path: Path, token: str) -> Submission | None:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE confirm_token = ?", (token,)
        ).fetchone()
    return _row_to_submission(row) if row else None


def confirm_submission(path: Path, token: str) -> Submission | None:
    """Flip PENDING_CONFIRMATION → QUEUED when the customer clicks the email link.

    Returns the updated row, or None if the token doesn't exist. If the row
    isn't PENDING_CONFIRMATION (already confirmed, or in a later state), the
    row is returned unchanged — callers should branch on the status to show
    the right page.
    """
    confirmed_at = _now_iso()
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE confirm_token = ?", (token,)
        ).fetchone()
        if row is None:
            return None
        if row["status"] != SubmissionStatus.PENDING_CONFIRMATION.value:
            return _row_to_submission(row)
        conn.execute(
            "UPDATE submissions SET status = ?, confirmed_at = ?"
            " WHERE confirm_token = ? AND status = ?",
            (
                SubmissionStatus.QUEUED.value,
                confirmed_at,
                token,
                SubmissionStatus.PENDING_CONFIRMATION.value,
            ),
        )
        row = conn.execute(
            "SELECT * FROM submissions WHERE confirm_token = ?", (token,)
        ).fetchone()
    return _row_to_submission(row)


def mark_packet_sent(path: Path, submission_id: str) -> None:
    """Record the moment the packet email was successfully sent."""
    sent_at = _now_iso()
    with _connect(path) as conn:
        conn.execute(
            "UPDATE submissions SET packet_sent_at = ? WHERE id = ?",
            (sent_at, submission_id),
        )


def claim_next_queued(path: Path) -> Submission | None:
    """Atomically grab the oldest QUEUED submission and flip it to RUNNING.

    Returns the row in its new RUNNING state, or None if the queue is empty.
    The UPDATE re-checks status to prevent two workers racing on the same row.
    """
    started_at = _now_iso()
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT id FROM submissions WHERE status = ?"
            " ORDER BY created_at ASC LIMIT 1",
            (SubmissionStatus.QUEUED.value,),
        ).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "UPDATE submissions SET status = ?, started_at = ?"
            " WHERE id = ? AND status = ?",
            (
                SubmissionStatus.RUNNING.value,
                started_at,
                row["id"],
                SubmissionStatus.QUEUED.value,
            ),
        )
        if cur.rowcount == 0:
            # Lost the race to another worker.
            return None
        claimed = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (row["id"],)
        ).fetchone()
    return _row_to_submission(claimed)


def mark_done(path: Path, submission_id: str) -> None:
    finished_at = _now_iso()
    with _connect(path) as conn:
        conn.execute(
            "UPDATE submissions SET status = ?, finished_at = ?, error = NULL"
            " WHERE id = ?",
            (SubmissionStatus.DONE.value, finished_at, submission_id),
        )


def mark_failed(path: Path, submission_id: str, *, error: str) -> None:
    finished_at = _now_iso()
    # Truncate so a runaway stack trace doesn't bloat the DB.
    error_short = error[:4000]
    with _connect(path) as conn:
        conn.execute(
            "UPDATE submissions SET status = ?, finished_at = ?, error = ?"
            " WHERE id = ?",
            (
                SubmissionStatus.FAILED.value,
                finished_at,
                error_short,
                submission_id,
            ),
        )


def list_submissions(
    path: Path, *, status: SubmissionStatus | None = None
) -> list[Submission]:
    """Return submissions oldest-first, optionally filtered by status."""
    with _connect(path) as conn:
        if status is not None:
            rows = conn.execute(
                "SELECT * FROM submissions WHERE status = ?"
                " ORDER BY created_at ASC",
                (status.value,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM submissions ORDER BY created_at ASC"
            ).fetchall()
    return [_row_to_submission(r) for r in rows]


def _row_to_submission(row: sqlite3.Row) -> Submission:
    def _dt(s: str | None) -> datetime | None:
        return datetime.fromisoformat(s) if s else None

    return Submission(
        id=row["id"],
        email=row["email"],
        quarter=row["quarter"],
        status=SubmissionStatus(row["status"]),
        confirm_token=row["confirm_token"],
        company=row["company"],
        error=row["error"],
        created_at=_dt(row["created_at"]) or datetime.now(UTC),
        confirmed_at=_dt(row["confirmed_at"]),
        started_at=_dt(row["started_at"]),
        finished_at=_dt(row["finished_at"]),
        packet_sent_at=_dt(row["packet_sent_at"]),
    )
