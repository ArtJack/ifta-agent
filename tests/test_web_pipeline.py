"""Tests for the web-intake pipeline driver.

Uses the existing Q4-2025 Menshikov fixture as a real end-to-end check —
same data the historical-accuracy regression covers, just driven through
the web submission entry point.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ifta.web.models import Submission, SubmissionStatus
from ifta.web.pipeline import PipelineError, process_submission

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_CSV = ROOT / "inbox" / "Q4-2025" / "menshikov_miles_and_fuel.csv"


def _make_submission(sid: str, quarter: str = "Q4-2025") -> Submission:
    return Submission(
        id=sid,
        email="customer@example.com",
        quarter=quarter,
        status=SubmissionStatus.RUNNING,
        confirm_token="tok",
        created_at=datetime.now(UTC),
        company="MENSHIKOV LLC",
    )


def _stage_fixture(submissions_dir: Path, sid: str, quarter: str = "Q4-2025") -> Path:
    inbox = submissions_dir / sid / "inbox" / quarter
    inbox.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE_CSV, inbox / FIXTURE_CSV.name)
    return inbox


def test_process_submission_produces_packet(tmp_path: Path) -> None:
    sid = "test_packet"
    _stage_fixture(tmp_path, sid)
    sub = _make_submission(sid)

    out_dir = process_submission(tmp_path, sub)

    assert out_dir == tmp_path / sid / "outputs" / "Q4-2025"
    portal = out_dir / "ifta_portal.csv"
    assert portal.exists() and portal.stat().st_size > 0
    review = out_dir / "review_note.md"
    assert review.exists()
    text = review.read_text(encoding="utf-8")
    # Quarter is rendered in canonical "4Q2025" form by compute_return.
    assert "4Q2025" in text or "Q4-2025" in text
    # Menshikov Q4-2025 — known $795.16 total tax due.
    assert "795.16" in text

    trucks_dir = out_dir / "trucks"
    assert trucks_dir.exists()
    truck_files = list(trucks_dir.glob("*.xlsx"))
    assert truck_files, "expected at least one per-truck Excel"


def test_process_submission_missing_inbox_raises(tmp_path: Path) -> None:
    sub = _make_submission("nope")
    with pytest.raises(PipelineError, match="inbox not found"):
        process_submission(tmp_path, sub)


def test_process_submission_empty_inbox_raises(tmp_path: Path) -> None:
    sid = "empty"
    inbox = tmp_path / sid / "inbox" / "Q4-2025"
    inbox.mkdir(parents=True)
    # Empty inbox — no usable data
    sub = _make_submission(sid)
    with pytest.raises(PipelineError):
        process_submission(tmp_path, sub)
