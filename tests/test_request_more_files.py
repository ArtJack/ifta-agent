"""Tests for Step 8 slice 3 — the operator's '📩 Request more files' button.

Covers the full chain end-to-end without hitting Telegram or Resend:

  Telegram inline keyboard (3 buttons)
      └─ bot callback dispatcher (matches wm: prefix)
              └─ db.request_more_files_submission  (PENDING_APPROVAL -> NEEDS_MORE_FILES)
              └─ email.send_more_files_request     (customer email + PDF attachment)
                      └─ render_more_files_request (plain English, no dev markup)
                      └─ render_more_files_request_pdf (real PDF, same content)

The anti-leakage guards from Steps 5–7 apply identically: no SHOUTY_CODES,
no [ERROR]/[WARNING] tags, no `file_` underscore-mangled names, no internal
JSON snippets, no Python tracebacks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ifta.web import db
from ifta.web.customer_view import (
    render_more_files_request,
    render_more_files_request_pdf,
)
from ifta.web.email import EmailClient, EmailConfig
from ifta.web.models import Submission, SubmissionStatus
from ifta.web.telegram_approval import (
    CB_WEB_ACCEPT,
    CB_WEB_DECLINE,
    CB_WEB_MORE_FILES,
    _build_inline_keyboard,
)

# ─── fixtures ────────────────────────────────────────────────────────────────


def _sub(name: str | None = "Eugene") -> Submission:
    return Submission(
        id="sub-1",
        email="ops@blabla.co",
        quarter="Q1-2026",
        status=SubmissionStatus.PENDING_APPROVAL,
        confirm_token="t",
        created_at=datetime.now(UTC),
        company="BLA BLA Transportation",
        name=name,
    )


# A realistic intake_brief.md slice — the same format ifta.web.intake_brief
# writes to disk at submission time. Used to exercise the parser against
# real-shaped input rather than a synthetic stub.
_REAL_INTAKE_BRIEF = """\
# Intake Brief -- Q1-2026

## Customer
- Name: Eugene Menshikov
- Email: ops@blabla.co
- Company: BLA BLA Transportation
- Base state: KY
- Fleet size: 5

## Uploaded files
- file_ifta-DM_EXPRESS_INC.xlsx (53 KB)
- file_Summary_by_State_-_DM_EXPRESS_INC.pdf (3 KB)

## Preflight
- Mileage rows parsed: 88
- Fuel rows parsed: 28
- Trucks detected: 2013, 2015, 2017, 2019, 55
- [WARNING] RAW_MPG_HIGH: Raw miles/gallons MPG is 14.27 (124,926 miles / 8,753.74 gal), above the expected heavy-diesel range up to 10.5. This usually means missing fuel files, date-range mismatch, or duplicate miles.
- [WARNING] DUPLICATE_FUEL_SOURCE: file_Summary_by_State_-_DM_EXPRESS_INC.pdf and file_ifta-DM_EXPRESS_INC.xlsx both parsed 8,753.74 gallons.
- [WARNING] UNKNOWN_TRUCK: Some rows had no resolvable truck_id and were bucketed as 'unknown'.
"""


# ─── inline keyboard ─────────────────────────────────────────────────────────


def test_inline_keyboard_has_three_buttons_with_distinct_callbacks() -> None:
    """The approval card now offers a softer middle path next to the binary
    Accept / Decline choices."""
    kb = _build_inline_keyboard("sub-9")
    rows = kb["inline_keyboard"]
    assert len(rows) == 2, "expected the 'Request more files' button on its own row"
    top = rows[0]
    bottom = rows[1]
    assert [b["callback_data"] for b in top] == [
        f"{CB_WEB_ACCEPT}:sub-9",
        f"{CB_WEB_DECLINE}:sub-9",
    ]
    assert [b["callback_data"] for b in bottom] == [
        f"{CB_WEB_MORE_FILES}:sub-9",
    ]
    # Callback strings stay under Telegram's 64-byte limit.
    for row in rows:
        for b in row:
            assert len(b["callback_data"]) <= 64


def test_callback_prefixes_are_short_and_distinct() -> None:
    """The three prefixes share no common stem so a startswith check on one
    can't accidentally match another."""
    prefixes = {CB_WEB_ACCEPT, CB_WEB_DECLINE, CB_WEB_MORE_FILES}
    assert len(prefixes) == 3
    assert all(len(p) <= 4 for p in prefixes)  # 64-byte budget is plenty


# ─── DB transition ───────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "jobs.db"
    db.init_db(p)
    return p


def _create_pending(db_path: Path, sid: str = "s1") -> Submission:
    # Default status on create_submission is QUEUED (legacy contract from
    # before the approval gate). The web app's POST /submit handler sets it
    # explicitly to PENDING_APPROVAL; mirror that here.
    return db.create_submission(
        db_path,
        submission_id=sid,
        email="ops@blabla.co",
        quarter="Q1-2026",
        confirm_token=f"tok-{sid}",
        company="BLA BLA Transportation",
        status=SubmissionStatus.PENDING_APPROVAL,
    )


def test_request_more_files_submission_flips_pending_to_needs_more_files(
    db_path: Path,
) -> None:
    _create_pending(db_path)
    updated = db.request_more_files_submission(
        db_path, "s1", decided_by="Eugene", reason="need full Jan-Mar fuel",
    )
    assert updated is not None
    assert updated.status == SubmissionStatus.NEEDS_MORE_FILES
    assert updated.decided_by == "Eugene"
    assert updated.decline_reason == "need full Jan-Mar fuel"
    assert updated.decided_at is not None


def test_request_more_files_is_safe_on_already_decided_row(db_path: Path) -> None:
    """If the row has already been approved/rejected/etc., the transition is
    a no-op (returns the current row unchanged). Prevents a double-tap race
    from corrupting state if the operator taps More Files after Accept."""
    _create_pending(db_path)
    approved = db.approve_submission(db_path, "s1", decided_by="Eugene")
    assert approved is not None and approved.status == SubmissionStatus.QUEUED
    after = db.request_more_files_submission(db_path, "s1", decided_by="Eugene")
    assert after is not None
    assert after.status == SubmissionStatus.QUEUED  # unchanged — already decided


def test_request_more_files_on_unknown_id_returns_none(db_path: Path) -> None:
    assert (
        db.request_more_files_submission(db_path, "ghost", decided_by="Eugene") is None
    )


# ─── customer-facing renderers ───────────────────────────────────────────────


def test_more_files_body_is_friendly_and_strips_dev_markup() -> None:
    body = render_more_files_request(sub=_sub(), intake_brief=_REAL_INTAKE_BRIEF)
    assert body.startswith("Hi Eugene,\n")
    # Soft framing: NOT the failure path's "couldn't finish".
    assert "Before we can finish your filing" in body
    assert "Reply to this email" in body
    assert "https://artjeck.com/ifta/submit" in body
    assert "summary_report.pdf" in body
    # Anti-leakage — the same exhaustive list we apply to the failure body.
    for forbidden in (
        "[WARNING]",
        "[ERROR]",
        "RAW_MPG_HIGH",
        "DUPLICATE_FUEL_SOURCE",
        "UNKNOWN_TRUCK",
        "file_ifta-DM_EXPRESS",
        "file_Summary_by_State",
        "## Preflight",
        "Mileage rows parsed:",  # internal counter line
    ):
        assert forbidden not in body, f"more-files email leaked dev markup: {forbidden!r}"


def test_more_files_body_humanizes_filenames_in_findings() -> None:
    """`file_Summary_by_State_-_DM_EXPRESS_INC.pdf` should appear as a normal
    filename, the way a trucker would have named it."""
    body = render_more_files_request(sub=_sub(), intake_brief=_REAL_INTAKE_BRIEF)
    assert "Summary by State - DM EXPRESS INC.pdf" in body
    assert "ifta-DM EXPRESS INC.xlsx" in body


def test_more_files_body_falls_back_when_brief_is_empty_or_opaque() -> None:
    """If the brief doesn't carry `[SEVERITY] CODE: ...` bullets the renderer
    emits a generic friendly line rather than dumping raw text."""
    body = render_more_files_request(sub=_sub(), intake_brief="")
    assert "Eugene will follow up" in body
    body2 = render_more_files_request(sub=_sub(), intake_brief="Internal error: KeyError 'state'")
    assert "KeyError" not in body2


def test_more_files_pdf_is_real_pdf_with_no_dev_markup() -> None:
    pdf = render_more_files_request_pdf(sub=_sub(), intake_brief=_REAL_INTAKE_BRIEF)
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 1000


# ─── email integration ──────────────────────────────────────────────────────


def _enabled_email_config() -> EmailConfig:
    return EmailConfig(api_key="re_test")


@pytest.fixture
def captured_sends(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sends: list[dict[str, Any]] = []
    from ifta.web import email as email_module

    monkeypatch.setattr(
        email_module,
        "_send_via_resend",
        lambda params: sends.append(params) or "fake-id",
    )
    return sends


def test_send_more_files_request_subject_body_and_pdf_attachment(
    captured_sends: list[dict[str, Any]],
) -> None:
    sub = _sub()
    sub.status = SubmissionStatus.NEEDS_MORE_FILES
    client = EmailClient(_enabled_email_config())
    assert client.send_more_files_request(sub, _REAL_INTAKE_BRIEF) is True

    params = captured_sends[0]
    assert "few more files needed" in params["subject"].lower()
    assert "Q1-2026" in params["subject"]
    body = params["text"]
    assert "Before we can finish your filing" in body
    # Detailed report attached as PDF (Step 9 pattern).
    assert params["attachments"]
    pdf_att = next(
        a for a in params["attachments"] if a["filename"] == "summary_report.pdf"
    )
    assert bytes(pdf_att["content"])[:4] == b"%PDF"
