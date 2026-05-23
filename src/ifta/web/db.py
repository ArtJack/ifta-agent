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
    telegram_message_id INTEGER,
    telegram_chat_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_status ON submissions(status);
CREATE INDEX IF NOT EXISTS idx_confirm_token ON submissions(confirm_token);
"""

# Columns added after the original schema shipped. SQLite has no
# `ADD COLUMN IF NOT EXISTS`, so we inspect PRAGMA table_info and ALTER any
# missing column on every init. Order is irrelevant; types match SCHEMA above.
_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("name", "TEXT"),
    ("base_state", "TEXT"),
    ("fleet_size", "INTEGER"),
    ("notes", "TEXT"),
    ("intake_brief_path", "TEXT"),
    ("intake_summary", "TEXT"),
    ("decided_at", "TEXT"),
    ("decided_by", "TEXT"),
    ("decline_reason", "TEXT"),
    ("telegram_message_id", "INTEGER"),
    ("telegram_chat_id", "INTEGER"),
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def init_db(path: Path) -> None:
    """Create the schema (idempotent) and enable WAL.

    Also applies forward-only column migrations so older DBs gain the columns
    added by the intake-brief / Telegram-approval flow without a manual
    rebuild step.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        existing = {row[1] for row in conn.execute("PRAGMA table_info(submissions)")}
        for col, col_type in _MIGRATIONS:
            if col not in existing:
                conn.execute(f"ALTER TABLE submissions ADD COLUMN {col} {col_type}")


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
    name: str | None = None,
    base_state: str | None = None,
    fleet_size: int | None = None,
    notes: str | None = None,
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
            " (id, email, quarter, status, confirm_token, company, created_at,"
            "  name, base_state, fleet_size, notes)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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


def approve_submission(
    path: Path,
    submission_id: str,
    *,
    decided_by: str,
) -> Submission | None:
    """Flip PENDING_APPROVAL -> QUEUED when an operator taps Accept."""
    decided_at = _now_iso()
    with _connect(path) as conn:
        cur = conn.execute(
            "UPDATE submissions SET status = ?, decided_at = ?, decided_by = ?"
            " WHERE id = ? AND status = ?",
            (
                SubmissionStatus.QUEUED.value,
                decided_at,
                decided_by,
                submission_id,
                SubmissionStatus.PENDING_APPROVAL.value,
            ),
        )
        if cur.rowcount == 0:
            # Already decided or unknown id.
            row = conn.execute(
                "SELECT * FROM submissions WHERE id = ?", (submission_id,)
            ).fetchone()
            return _row_to_submission(row) if row else None
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
    return _row_to_submission(row) if row else None


def reopen_for_review(
    path: Path,
    submission_id: str,
) -> Submission | None:
    """Flip NEEDS_MORE_FILES (or PENDING_APPROVAL — no-op) back to PENDING_APPROVAL.

    Called from the /submit/add/{token} endpoint after the customer uploads
    more files: the submission is ready for a fresh operator decision based
    on the updated inbox. Clears the prior decided_at/by/reason so the new
    approval card represents the current state, not the previous ask.
    """
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
        if row is None:
            return None
        if row["status"] not in (
            SubmissionStatus.NEEDS_MORE_FILES.value,
            SubmissionStatus.PENDING_APPROVAL.value,
        ):
            # Row already moved on (queued, running, done, rejected, failed) —
            # the customer's add-files window has closed. Return the current
            # row so the caller can surface the right HTTP code.
            return _row_to_submission(row)
        conn.execute(
            "UPDATE submissions SET status = ?, decided_at = NULL, decided_by = NULL,"
            " decline_reason = NULL WHERE id = ?",
            (SubmissionStatus.PENDING_APPROVAL.value, submission_id),
        )
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
    return _row_to_submission(row) if row else None


def request_more_files_submission(
    path: Path,
    submission_id: str,
    *,
    decided_by: str,
    reason: str = "",
) -> Submission | None:
    """Flip PENDING_APPROVAL -> NEEDS_MORE_FILES when an operator taps the
    Telegram "Request more files" button. The submission stays open; the
    customer is emailed a plain-English ask for the missing pieces and can
    re-upload through artjeck.com to advance it (Step 8 slice 3)."""
    decided_at = _now_iso()
    reason_short = (reason or "")[:2000]
    with _connect(path) as conn:
        cur = conn.execute(
            "UPDATE submissions SET status = ?, decided_at = ?, decided_by = ?,"
            " decline_reason = ?"
            " WHERE id = ? AND status = ?",
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
                "SELECT * FROM submissions WHERE id = ?", (submission_id,)
            ).fetchone()
            return _row_to_submission(row) if row else None
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
    return _row_to_submission(row) if row else None


def reject_submission(
    path: Path,
    submission_id: str,
    *,
    decided_by: str,
    reason: str,
) -> Submission | None:
    """Flip PENDING_APPROVAL -> REJECTED when an operator taps Decline."""
    decided_at = _now_iso()
    reason_short = reason[:2000]
    with _connect(path) as conn:
        cur = conn.execute(
            "UPDATE submissions SET status = ?, decided_at = ?, decided_by = ?,"
            " decline_reason = ?"
            " WHERE id = ? AND status = ?",
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
                "SELECT * FROM submissions WHERE id = ?", (submission_id,)
            ).fetchone()
            return _row_to_submission(row) if row else None
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
    return _row_to_submission(row) if row else None


def update_telegram_card(
    path: Path,
    submission_id: str,
    *,
    message_id: int,
    chat_id: int,
) -> None:
    """Store the Telegram message/chat IDs so the card can be edited later."""
    with _connect(path) as conn:
        conn.execute(
            "UPDATE submissions SET telegram_message_id = ?, telegram_chat_id = ?"
            " WHERE id = ?",
            (message_id, chat_id, submission_id),
        )


def update_intake_brief(
    path: Path,
    submission_id: str,
    *,
    brief_path: str,
    summary: str,
) -> None:
    """Store the intake brief path and summary text."""
    with _connect(path) as conn:
        conn.execute(
            "UPDATE submissions SET intake_brief_path = ?, intake_summary = ?"
            " WHERE id = ?",
            (brief_path, summary, submission_id),
        )


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
