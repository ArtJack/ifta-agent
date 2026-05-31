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
from ifta.web.pipeline import (
    FINDINGS_FILENAME,
    PipelineError,
    load_findings,
    process_submission,
    summarize_warnings,
)

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


def test_process_submission_produces_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pipeline now optionally invokes the agent. For this fast test we use
    # the deterministic-only path; agent invocation is covered separately.
    monkeypatch.setenv("IFTA_WEB_SKIP_AGENT", "1")

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

    # BUG-002: the .md (operator) and .pdf (customer) summaries must coexist —
    # they shared a filename, so the PDF write used to clobber the markdown.
    md_summary = out_dir / "summary_report.md"
    pdf_summary = out_dir / "summary_report.pdf"
    assert md_summary.exists() and md_summary.stat().st_size > 0
    assert pdf_summary.exists() and pdf_summary.stat().st_size > 0


def test_process_submission_missing_inbox_raises(tmp_path: Path) -> None:
    sub = _make_submission("nope")
    with pytest.raises(PipelineError, match="inbox not found"):
        process_submission(tmp_path, sub)


def test_process_submission_writes_findings_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IFTA_WEB_SKIP_AGENT", "1")
    sid = "test_findings"
    _stage_fixture(tmp_path, sid)
    out_dir = process_submission(tmp_path, _make_submission(sid))

    findings_path = out_dir / FINDINGS_FILENAME
    assert findings_path.exists()
    items = load_findings(out_dir)
    assert isinstance(items, list)
    # Every item carries a uniform shape regardless of source.
    for it in items:
        assert {"source", "severity", "code"} <= set(it)


def test_load_findings_missing_returns_empty(tmp_path: Path) -> None:
    assert load_findings(tmp_path) == []


def test_summarize_warnings_dedupes_and_filters_severity() -> None:
    findings = [
        {"severity": "info", "code": "OREGON_WMT", "state": "OR"},
        {"severity": "warning", "code": "FUEL_NO_MILES", "state": "WY"},
        {"severity": "warning", "code": "FUEL_NO_MILES", "state": "WY"},  # dup
        {"severity": "warning", "code": "MPG_HIGH", "state": None},
        {"severity": "error", "code": "DUPLICATE_FUEL_SOURCE"},
    ]
    labels = summarize_warnings(findings)
    assert labels == ["FUEL_NO_MILES (WY)", "MPG_HIGH", "DUPLICATE_FUEL_SOURCE"]
    # Info-level is excluded.
    assert all("OREGON_WMT" not in label for label in labels)


def test_process_submission_invokes_agent_when_key_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pipeline should call agent_review with explicit inbox/output paths."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake")
    monkeypatch.delenv("IFTA_WEB_SKIP_AGENT", raising=False)

    sid = "agent_run"
    _stage_fixture(tmp_path, sid)
    sub = _make_submission(sid)

    captured: dict[str, object] = {}

    def fake_review(quarter, **kwargs):
        captured["quarter"] = quarter
        captured.update(kwargs)
        from ifta.agent.metrics import AgentMetrics
        from ifta.agent.runner import ReviewNote

        note = ReviewNote(
            summary="Anonymous submission OK; numbers reconcile.",
            issues=[],
            filing_reminders=[],
            next_steps=[],
        )
        return note, AgentMetrics(model="claude-opus-4-7")

    monkeypatch.setattr("ifta.web.pipeline.agent_review", fake_review, raising=False)
    # Patch where the lazy import lands inside _write_agent_review.
    import ifta.agent as agent_pkg

    monkeypatch.setattr(agent_pkg, "review", fake_review)

    out_dir = process_submission(tmp_path, sub)

    assert captured["inbox_dir"] == tmp_path / sid / "inbox" / "Q4-2025"
    assert captured["output_dir"] == out_dir
    assert captured["client_name"] == "MENSHIKOV LLC"
    # The agent's note ends up in review_note.md
    review_text = (out_dir / "review_note.md").read_text(encoding="utf-8")
    assert "Anonymous submission OK" in review_text


def test_process_submission_falls_back_when_agent_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the agent raises, the customer still gets the deterministic packet."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake")
    monkeypatch.delenv("IFTA_WEB_SKIP_AGENT", raising=False)

    sid = "agent_fail"
    _stage_fixture(tmp_path, sid)
    sub = _make_submission(sid)

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated agent outage")

    import ifta.agent as agent_pkg

    monkeypatch.setattr(agent_pkg, "review", boom)

    out_dir = process_submission(tmp_path, sub)
    # Packet still produced; review_note.md falls back to deterministic copy.
    text = (out_dir / "review_note.md").read_text(encoding="utf-8")
    assert "795.16" in text
    assert "deterministic pipeline output only" in text


def test_process_submission_empty_inbox_raises(tmp_path: Path) -> None:
    sid = "empty"
    inbox = tmp_path / sid / "inbox" / "Q4-2025"
    inbox.mkdir(parents=True)
    # Empty inbox — no usable data
    sub = _make_submission(sid)
    with pytest.raises(PipelineError):
        process_submission(tmp_path, sub)
