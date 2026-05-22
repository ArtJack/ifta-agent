"""SQLite-backed job state for web intake.

One connection per call — SQLite handles single-writer concurrency fine for
the volumes we expect (one customer at a time). WAL mode is enabled at init
so readers (status endpoint) never block the worker.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
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
    trucks INTEGER,
    error TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    approved_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    packet_sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON submissions(status);
CREATE INDEX IF NOT EXISTS idx_confirm_token ON submissions(confirm_token);
"""

# Columns added after the first schema shipped. Each is applied with
# ALTER TABLE ADD COLUMN at init so existing databases upgrade in place.
_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("trucks", "ALTER TABLE submissions ADD COLUMN trucks INTEGER"),
    ("approved_at", "ALTER TABLE submissions ADD COLUMN approved_at TEXT"),
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def init_db(path: Path) -> None:
    """Create the schema (idempotent), apply migrations, and enable WAL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        existing = {row[1] for row in conn.execute("PRAGMA table_info(submissions)")}
        for column, ddl in _MIGRATIONS:
            if column not in existing:
                conn.execute(ddl)


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
    trucks: int | None = None,
    status: SubmissionStatus = SubmissionStatus.PENDING_APPROVAL,
) -> Submission:
    """Insert a new submission row.

    The default status is PENDING_APPROVAL: submissions wait for the operator
    to approve them (via the Telegram approval bot) before the worker runs the
    full pipeline. Approval flips the row to QUEUED; rejection flips it to
    REJECTED. Pass status=QUEUED explicitly to bypass the gate (e.g. tests).
    """
    created_at_iso = _now_iso()
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO submissions"
            " (id, email, quarter, status, confirm_token, company, trucks, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                submission_id,
                email,
                quarter,
                status.value,
                confirm_token,
                company,
                trucks,
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
        trucks=trucks,
        created_at=datetime.fromisoformat(created_at_iso),
    )


def set_trucks(path: Path, submission_id: str, trucks: int) -> None:
    """Persist the preflight truck count used in the approval request."""
    with _connect(path) as conn:
        conn.execute(
            "UPDATE submissions SET trucks = ? WHERE id = ?",
            (trucks, submission_id),
        )


def approve_submission(path: Path, submission_id: str) -> Submission | None:
    """Flip PENDING_APPROVAL → QUEUED when the operator approves.

    Returns the updated row, or None if the id doesn't exist. If the row is
    not PENDING_APPROVAL (already approved/rejected/processed), it is returned
    unchanged so callers can branch on the status.
    """
    approved_at = _now_iso()
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
        if row is None:
            return None
        if row["status"] != SubmissionStatus.PENDING_APPROVAL.value:
            return _row_to_submission(row)
        conn.execute(
            "UPDATE submissions SET status = ?, approved_at = ?"
            " WHERE id = ? AND status = ?",
            (
                SubmissionStatus.QUEUED.value,
                approved_at,
                submission_id,
                SubmissionStatus.PENDING_APPROVAL.value,
            ),
        )
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
    return _row_to_submission(row)


def reject_submission(path: Path, submission_id: str) -> Submission | None:
    """Flip PENDING_APPROVAL → REJECTED when the operator declines.

    Returns the updated row, or None if the id doesn't exist. A row that is
    not PENDING_APPROVAL is returned unchanged.
    """
    finished_at = _now_iso()
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
        if row is None:
            return None
        if row["status"] != SubmissionStatus.PENDING_APPROVAL.value:
            return _row_to_submission(row)
        conn.execute(
            "UPDATE submissions SET status = ?, finished_at = ?"
            " WHERE id = ? AND status = ?",
            (
                SubmissionStatus.REJECTED.value,
                finished_at,
                submission_id,
                SubmissionStatus.PENDING_APPROVAL.value,
            ),
        )
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
    return _row_to_submission(row)


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
    """Flip PENDING_CONFIRMATION → PENDING_APPROVAL when the customer clicks the link.

    Email confirmation proves the address is real; the submission then waits
    for the operator to approve it (Telegram approval gate) before the worker
    runs. Returns the updated row, or None if the token doesn't exist. If the
    row isn't PENDING_CONFIRMATION (already confirmed, or in a later state),
    the row is returned unchanged — callers branch on status to show the right
    page and to fire the approval request only on the first transition.
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
                SubmissionStatus.PENDING_APPROVAL.value,
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


def reap_stale_running(
    path: Path, *, max_seconds_running: int = 900
) -> list[Submission]:
    """Mark RUNNING rows older than the cutoff as FAILED, return them.

    Covers the case where the worker process is killed mid-job (OOM, SIGKILL,
    host reboot, launchd kickstart) — the row stays in RUNNING forever and is
    never picked up by claim_next_queued. Called at worker startup so each
    fresh process recovers the previous run's orphans before polling.
    """
    cutoff_iso = (datetime.now(UTC) - timedelta(seconds=max_seconds_running)).isoformat()
    error_text = (
        "Worker stopped while this submission was being processed. "
        "It will not retry automatically — re-submit at artjeck.com/ifta/submit."
    )
    finished_at = _now_iso()
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM submissions WHERE status = ? AND started_at < ?",
            (SubmissionStatus.RUNNING.value, cutoff_iso),
        ).fetchall()
        if not rows:
            return []
        conn.execute(
            "UPDATE submissions SET status = ?, finished_at = ?, error = ?"
            " WHERE status = ? AND started_at < ?",
            (
                SubmissionStatus.FAILED.value,
                finished_at,
                error_text,
                SubmissionStatus.RUNNING.value,
                cutoff_iso,
            ),
        )
    return [_row_to_submission(r) for r in rows]


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

    keys = row.keys()
    return Submission(
        id=row["id"],
        email=row["email"],
        quarter=row["quarter"],
        status=SubmissionStatus(row["status"]),
        confirm_token=row["confirm_token"],
        company=row["company"],
        trucks=row["trucks"] if "trucks" in keys else None,
        error=row["error"],
        created_at=_dt(row["created_at"]) or datetime.now(UTC),
        confirmed_at=_dt(row["confirmed_at"]),
        approved_at=_dt(row["approved_at"]) if "approved_at" in keys else None,
        started_at=_dt(row["started_at"]),
        finished_at=_dt(row["finished_at"]),
        packet_sent_at=_dt(row["packet_sent_at"]),
    )
