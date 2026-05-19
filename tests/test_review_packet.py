from __future__ import annotations

import pytest

from ifta.agent.runner import ReviewNote, _enforce_deterministic_filing_status
from ifta.calc import compute_return
from ifta.client import ClientContext
from ifta.models import CleanData, FuelRecord, MileageRecord
from ifta.rates import RateTable
from ifta.review_packet import build_review_packet, determine_filing_status
from ifta.validator import Finding


def _rates(*, fallback: bool = False) -> RateTable:
    return RateTable(
        quarter="Q2-2026",
        fuel="diesel",
        rates={"CA": 0.971, "NV": 0.27, "OR": 0.0},
        surcharge_rates={},
        requested_quarter="2Q2026",
        source_quarter="1Q2026" if fallback else "2Q2026",
        fallback_used=fallback,
        warning="2Q2026 rates unavailable; using 1Q2026." if fallback else None,
    )


def _data() -> CleanData:
    return CleanData(
        miles=[
            MileageRecord("T1", "CA", 1000),
            MileageRecord("T1", "NV", 500),
            MileageRecord("T2", "OR", 300),
        ],
        fuel=[
            FuelRecord("T1", "CA", 100),
            FuelRecord("T1", "OR", 50),
            FuelRecord("T2", "CA", 40),
        ],
    )


def test_filing_status_blocks_on_rate_fallback() -> None:
    ret = compute_return(_data(), _rates(fallback=True))
    findings = [Finding("warning", "RATE_FALLBACK", "Fallback rates used.")]

    status = determine_filing_status(ret, findings)

    assert status["status"] == "DO_NOT_FILE"
    assert any("rates unavailable" in reason for reason in status["reasons"])


def test_filing_status_allows_warnings_but_not_clean_ready() -> None:
    ret = compute_return(_data(), _rates())
    findings = [Finding("warning", "FUEL_NO_MILES", "Fuel without miles.", state="OR")]

    status = determine_filing_status(ret, findings)

    assert status["status"] == "READY_WITH_WARNINGS"
    assert status["reasons"] == ["[FUEL_NO_MILES] Fuel without miles."]


def test_review_packet_contains_evidence_sections() -> None:
    data = _data()
    ret = compute_return(data, _rates())
    findings = [Finding("warning", "FUEL_NO_MILES", "Fuel without miles.", state="OR")]

    packet = build_review_packet(
        data,
        ret,
        findings,
        ClientContext(client_id="test", client_name="TEST LLC", portal="generic"),
    )

    assert packet["filing_status"]["status"] == "READY_WITH_WARNINGS"
    assert packet["return_summary"]["fleet_miles"] == 1800
    assert packet["validator_findings"][0]["code"] == "FUEL_NO_MILES"
    assert packet["fuel_without_miles"]
    assert packet["miles_without_fuel"]
    assert packet["review_output_schema"]["issues"][0]["evidence"]


def test_deterministic_status_overrides_model_status() -> None:
    note = ReviewNote(
        summary="Looks ready.",
        issues=[],
        filing_reminders=[],
        next_steps=[],
        filing_status="READY_TO_FILE",
    )

    _enforce_deterministic_filing_status(
        note,
        {
            "status": "DO_NOT_FILE",
            "reasons": ["[RATE_FALLBACK] Fallback rates used."],
        },
    )

    assert note.filing_status == "DO_NOT_FILE"
    assert note.filing_status_reasons == ["[RATE_FALLBACK] Fallback rates used."]
    assert note.issues
    assert note.issues[0]["code"] == "FILING_STATUS_OVERRIDE"


def test_empty_packet_status_raises_rather_than_silently_bypassing() -> None:
    note = ReviewNote(
        summary="Looks ready.",
        issues=[],
        filing_reminders=[],
        next_steps=[],
        filing_status="READY_TO_FILE",
    )

    with pytest.raises(ValueError, match="filing_status.status is empty"):
        _enforce_deterministic_filing_status(note, {"status": "", "reasons": []})


def test_packet_status_enforced_when_model_omits_status() -> None:
    note = ReviewNote(
        summary="No opinion offered.",
        issues=[],
        filing_reminders=[],
        next_steps=[],
        filing_status=None,
    )

    _enforce_deterministic_filing_status(
        note,
        {
            "status": "DO_NOT_FILE",
            "reasons": ["[RATE_FALLBACK] Fallback rates used."],
        },
    )

    assert note.filing_status == "DO_NOT_FILE"
    assert note.filing_status_reasons == ["[RATE_FALLBACK] Fallback rates used."]
