"""PostgreSQL job-state backend (Azure). Mirrors ``ifta.web.db_sqlite``.

Selected by ``ifta.web.db`` when ``IFTA_WEB_DB_URL`` is set. The DSN is read
from that variable on each connection, e.g.::

    postgresql://user:pass@host:5432/ifta?sslmode=require

Timestamps are stored as ISO-8601 TEXT — identical to the SQLite backend — so
the row→Submission mapping and the ``ORDER BY created_at`` / ``started_at <``
comparisons behave the same. The queue claim uses
``SELECT ... FOR UPDATE SKIP LOCKED`` so multiple workers drain safely without
racing on the same row.

The leading ``path`` argument on each function exists only for signature parity
with the SQLite backend; Postgres ignores it and uses the DSN from the
environment.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from ifta.web.models import Submission, SubmissionStatus

_CREATE_TABLE = """
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
    packet_sent_at TEXT,
    name TEXT,
    base_state TEXT,
    fleet_size INTEGER,
    notes TEXT,
    intake_brief_path TEXT,
    intake_summary TEXT,
    decided_at TEXT,
    decided_by TEXT,
    decline_reason TEXT,
    telegram_message_id BIGINT,
    telegram_chat_id BIGINT
)
"""

_CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_status ON submissions(status)",
    "CREATE INDEX IF NOT EXISTS idx_confirm_token ON submissions(confirm_token)",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _dsn() -> str:
    dsn = os.environ.get("IFTA_WEB_DB_URL")
    if not dsn:
        raise RuntimeError(
            "IFTA_WEB_DB_URL is not set but the Postgres backend was selected"
        )
    return dsn


@contextmanager
def _connect() -> Iterator[psycopg.Connection]:
    """One connection per call, committed on clean exit (mirrors SQLite backend)."""
    conn = psycopg.connect(_dsn(), row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(path: Path) -> None:
    """Create the schema (idempotent). ``path`` is ignored (signature parity)."""
    with _connect() as conn:
        conn.execute(_CREATE_TABLE)
        for stmt in _CREATE_INDEXES:
            conn.execute(stmt)


def create_submission(
    path: Path,
    *,
    submission_id: str,
    email: str,
    quarter: str,
    confirm_token: str,
    company: str | None = None,
    status: SubmissionStatus = SubmissionStatus.QUEUED,
    name: str | None = None,
    base_state: str | None = None,
    fleet_size: int | None = None,
    notes: str | None = None,
) -> Submission:
    created_at_iso = _now_iso()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO submissions"
            " (id, email, quarter, status, confirm_token, company, created_at,"
            "  name, base_state, fleet_size, notes)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                submission_id,
                email,
                quarter,
                status.value,
                confirm_token,
                company,
                created_at_iso,
                name,
                base_state,
                fleet_size,
                notes,
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
        name=name,
        base_state=base_state,
        fleet_size=fleet_size,
        notes=notes,
    )


def get_submission(path: Path, submission_id: str) -> Submission | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = %s", (submission_id,)
        ).fetchone()
    return _row_to_submission(row) if row else None


def get_submission_by_token(path: Path, token: str) -> Submission | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE confirm_token = %s", (token,)
        ).fetchone()
    return _row_to_submission(row) if row else None


def confirm_submission(path: Path, token: str) -> Submission | None:
    """Flip PENDING_CONFIRMATION → QUEUED when the customer clicks the email link."""
    confirmed_at = _now_iso()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE confirm_token = %s", (token,)
        ).fetchone()
        if row is None:
            return None
        if row["status"] != SubmissionStatus.PENDING_CONFIRMATION.value:
            return _row_to_submission(row)
        conn.execute(
            "UPDATE submissions SET status = %s, confirmed_at = %s"
            " WHERE confirm_token = %s AND status = %s",
            (
                SubmissionStatus.QUEUED.value,
                confirmed_at,
                token,
                SubmissionStatus.PENDING_CONFIRMATION.value,
            ),
        )
        row = conn.execute(
            "SELECT * FROM submissions WHERE confirm_token = %s", (token,)
        ).fetchone()
    return _row_to_submission(row)


def mark_packet_sent(path: Path, submission_id: str) -> None:
    """Record the moment the packet email was successfully sent."""
    sent_at = _now_iso()
    with _connect() as conn:
        conn.execute(
            "UPDATE submissions SET packet_sent_at = %s WHERE id = %s",
            (sent_at, submission_id),
        )


def claim_next_queued(path: Path) -> Submission | None:
    """Atomically grab the oldest QUEUED submission and flip it to RUNNING.

    ``FOR UPDATE SKIP LOCKED`` holds a row lock for the duration of the
    transaction, so concurrent workers skip a row another worker has already
    claimed instead of blocking or double-processing it.
    """
    started_at = _now_iso()
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM submissions WHERE status = %s"
            " ORDER BY created_at ASC LIMIT 1"
            " FOR UPDATE SKIP LOCKED",
            (SubmissionStatus.QUEUED.value,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE submissions SET status = %s, started_at = %s WHERE id = %s",
            (SubmissionStatus.RUNNING.value, started_at, row["id"]),
        )
        claimed = conn.execute(
            "SELECT * FROM submissions WHERE id = %s", (row["id"],)
        ).fetchone()
    return _row_to_submission(claimed)


def mark_done(path: Path, submission_id: str) -> None:
    finished_at = _now_iso()
    with _connect() as conn:
        conn.execute(
            "UPDATE submissions SET status = %s, finished_at = %s, error = NULL"
            " WHERE id = %s AND status IN (%s, %s)",
            (
                SubmissionStatus.DONE.value,
                finished_at,
                submission_id,
                SubmissionStatus.RUNNING.value,
                SubmissionStatus.FAILED.value,
            ),
        )


def mark_failed(path: Path, submission_id: str, *, error: str) -> None:
    finished_at = _now_iso()
    error_short = error[:4000]
    with _connect() as conn:
        conn.execute(
            "UPDATE submissions SET status = %s, finished_at = %s, error = %s"
            " WHERE id = %s AND status NOT IN (%s, %s, %s)",
            (
                SubmissionStatus.FAILED.value,
                finished_at,
                error_short,
                submission_id,
                SubmissionStatus.DONE.value,
                SubmissionStatus.REJECTED.value,
                SubmissionStatus.FAILED.value,
            ),
        )


def reap_stale_running(
    path: Path, *, max_seconds_running: int = 900
) -> list[Submission]:
    """Mark RUNNING rows older than the cutoff as FAILED, return them."""
    cutoff_iso = (
        datetime.now(UTC) - timedelta(seconds=max_seconds_running)
    ).isoformat()
    error_text = (
        "Worker stopped while this submission was being processed. "
        "It will not retry automatically — re-submit at artjeck.com/ifta/submit."
    )
    finished_at = _now_iso()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM submissions WHERE status = %s AND started_at < %s",
            (SubmissionStatus.RUNNING.value, cutoff_iso),
        ).fetchall()
        if not rows:
            return []
        conn.execute(
            "UPDATE submissions SET status = %s, finished_at = %s, error = %s"
            " WHERE status = %s AND started_at < %s",
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
    with _connect() as conn:
        if status is not None:
            rows = conn.execute(
                "SELECT * FROM submissions WHERE status = %s ORDER BY created_at ASC",
                (status.value,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM submissions ORDER BY created_at ASC"
            ).fetchall()
    return [_row_to_submission(r) for r in rows]


def approve_submission(
    path: Path, submission_id: str, *, decided_by: str
) -> Submission | None:
    """Flip PENDING_APPROVAL -> QUEUED when an operator taps Accept."""
    decided_at = _now_iso()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE submissions SET status = %s, decided_at = %s, decided_by = %s"
            " WHERE id = %s AND status = %s",
            (
                SubmissionStatus.QUEUED.value,
                decided_at,
                decided_by,
                submission_id,
                SubmissionStatus.PENDING_APPROVAL.value,
            ),
        )
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT * FROM submissions WHERE id = %s", (submission_id,)
            ).fetchone()
            return _row_to_submission(row) if row else None
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = %s", (submission_id,)
        ).fetchone()
    return _row_to_submission(row) if row else None


def reopen_for_review(path: Path, submission_id: str) -> Submission | None:
    """Flip NEEDS_MORE_FILES (or PENDING_APPROVAL — no-op) back to PENDING_APPROVAL."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = %s", (submission_id,)
        ).fetchone()
        if row is None:
            return None
        if row["status"] not in (
            SubmissionStatus.NEEDS_MORE_FILES.value,
            SubmissionStatus.PENDING_APPROVAL.value,
        ):
            return _row_to_submission(row)
        conn.execute(
            "UPDATE submissions SET status = %s, decided_at = NULL, decided_by = NULL,"
            " decline_reason = NULL WHERE id = %s",
            (SubmissionStatus.PENDING_APPROVAL.value, submission_id),
        )
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = %s", (submission_id,)
        ).fetchone()
    return _row_to_submission(row) if row else None


def request_more_files_submission(
    path: Path, submission_id: str, *, decided_by: str, reason: str = ""
) -> Submission | None:
    """Flip PENDING_APPROVAL -> NEEDS_MORE_FILES on operator request."""
    decided_at = _now_iso()
    reason_short = (reason or "")[:2000]
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE submissions SET status = %s, decided_at = %s, decided_by = %s,"
            " decline_reason = %s WHERE id = %s AND status = %s",
            (
                SubmissionStatus.NEEDS_MORE_FILES.value,
                decided_at,
                decided_by,
                reason_short,
                submission_id,
                SubmissionStatus.PENDING_APPROVAL.value,
            ),
        )
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT * FROM submissions WHERE id = %s", (submission_id,)
            ).fetchone()
            return _row_to_submission(row) if row else None
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = %s", (submission_id,)
        ).fetchone()
    return _row_to_submission(row) if row else None


def reject_submission(
    path: Path, submission_id: str, *, decided_by: str, reason: str
) -> Submission | None:
    """Flip PENDING_APPROVAL -> REJECTED when an operator taps Decline."""
    decided_at = _now_iso()
    reason_short = reason[:2000]
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE submissions SET status = %s, decided_at = %s, decided_by = %s,"
            " decline_reason = %s WHERE id = %s AND status = %s",
            (
                SubmissionStatus.REJECTED.value,
                decided_at,
                decided_by,
                reason_short,
                submission_id,
                SubmissionStatus.PENDING_APPROVAL.value,
            ),
        )
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT * FROM submissions WHERE id = %s", (submission_id,)
            ).fetchone()
            return _row_to_submission(row) if row else None
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = %s", (submission_id,)
        ).fetchone()
    return _row_to_submission(row) if row else None


def update_telegram_card(
    path: Path, submission_id: str, *, message_id: int, chat_id: int
) -> None:
    """Store the Telegram message/chat IDs so the card can be edited later."""
    with _connect() as conn:
        conn.execute(
            "UPDATE submissions SET telegram_message_id = %s, telegram_chat_id = %s"
            " WHERE id = %s",
            (message_id, chat_id, submission_id),
        )


def update_intake_brief(
    path: Path, submission_id: str, *, brief_path: str, summary: str
) -> None:
    """Store the intake brief path and summary text."""
    with _connect() as conn:
        conn.execute(
            "UPDATE submissions SET intake_brief_path = %s, intake_summary = %s"
            " WHERE id = %s",
            (brief_path, summary, submission_id),
        )


def _row_to_submission(row: dict) -> Submission:
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
        name=row["name"],
        base_state=row["base_state"],
        fleet_size=row["fleet_size"],
        notes=row["notes"],
        intake_brief_path=row["intake_brief_path"],
        intake_summary=row["intake_summary"],
        decided_at=_dt(row["decided_at"]),
        decided_by=row["decided_by"],
        decline_reason=row["decline_reason"],
        telegram_message_id=row["telegram_message_id"],
        telegram_chat_id=row["telegram_chat_id"],
    )
