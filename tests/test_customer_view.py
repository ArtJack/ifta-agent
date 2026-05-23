"""Customer-facing email body renderer tests.

The point of this module is that a trucker or accountant can read the email
without confusion — no SHOUTY_CODES, no JSON `Evidence: {...}` blobs, no
"[warning] FOO_BAR:" prefixes. We guard against regressions on both fronts:
golden checks on the visible structure, and explicit "must NOT contain"
assertions on every shape of developer markup we've seen leak.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from ifta.web.customer_view import render_customer_view
from ifta.web.models import Submission, SubmissionStatus


def _sub(name: str | None = "Pat") -> Submission:
    return Submission(
        id="sub-1",
        email="pat@example.com",
        quarter="Q1-2026",
        status=SubmissionStatus.RUNNING,
        confirm_token="t",
        created_at=datetime.now(UTC),
        company="ACME LOGISTICS",
        name=name,
    )


def _ret(*, tax: float = 15.75, mpg: float = 6.67, warning: str | None = None) -> SimpleNamespace:
    """Stand-in for IftaReturn — we only read the few attributes used here."""
    return SimpleNamespace(total_tax_due=tax, fleet_mpg=mpg, rate_warning=warning)


# ─── basic shape ─────────────────────────────────────────────────────────────


def test_renders_minimal_ready_to_file() -> None:
    note = SimpleNamespace(filing_status="READY_TO_FILE", issues=[], next_steps=[])
    body = render_customer_view(sub=_sub(), ret=_ret(), note=note, truck_count=1)
    assert body.startswith("Hi Pat,\n")
    assert "Q1-2026 IFTA packet is ready" in body
    assert "Looks ready to file." in body
    assert "Total tax due: $15.75" in body
    assert "Fleet MPG: 6.67" in body
    assert "Before you file" not in body  # no warnings → no checklist
    assert "ifta_portal.csv" in body
    assert "trucks/<id>.xlsx" in body
    assert "Eugene" in body  # sign-off


def test_anonymous_greeting_when_no_name() -> None:
    note = SimpleNamespace(filing_status="READY_TO_FILE", issues=[], next_steps=[])
    body = render_customer_view(sub=_sub(name=None), ret=_ret(), note=note)
    assert body.startswith("Hi,\n")


def test_multi_truck_attachment_phrasing() -> None:
    note = SimpleNamespace(filing_status="READY_TO_FILE", issues=[], next_steps=[])
    body = render_customer_view(sub=_sub(), ret=_ret(), note=note, truck_count=5)
    assert "(5 trucks)" in body


# ─── warning rendering / dev-markup stripping ────────────────────────────────


def test_warning_claims_show_as_plain_bullets() -> None:
    note = SimpleNamespace(
        filing_status="READY_WITH_WARNINGS",
        issues=[
            {
                "severity": "warning",
                "code": "MILES_SUSPICIOUSLY_FEW",
                "claim": "Only 1 mileage row was parsed from file_miles.csv. A typical quarter has dozens.",
                "evidence": {"source": "inspect_raw_inputs.findings[MILES_SUSPICIOUSLY_FEW]", "value": "mile_rows=1"},
                "filing_impact": "Missing mileage rows would understate taxable gallons.",
                "recommended_action": "Confirm complete mileage.",
            },
        ],
        next_steps=[],
    )
    body = render_customer_view(sub=_sub(), ret=_ret(), note=note, truck_count=1)
    assert "Please double-check the items below before filing." in body
    assert "Before you file, please double-check:" in body
    assert "• Only 1 mileage row was parsed" in body
    # Critical: no dev markup leaks into the customer's email.
    assert "MILES_SUSPICIOUSLY_FEW" not in body
    assert "[warning]" not in body
    assert "Evidence" not in body
    assert "filing_impact" not in body
    assert "{" not in body  # no JSON anywhere


def test_next_steps_dedup_against_issues() -> None:
    """If the agent repeats a warning's claim in next_steps' recommended_action,
    the customer sees it once — not twice."""
    note = SimpleNamespace(
        filing_status="READY_WITH_WARNINGS",
        issues=[
            {"severity": "warning", "code": "X", "claim": "Confirm the mileage covers the full quarter."},
        ],
        next_steps=[
            {"severity": "warning", "code": "X", "recommended_action": "Confirm the mileage covers the full quarter."},
        ],
    )
    body = render_customer_view(sub=_sub(), ret=_ret(), note=note)
    assert body.count("Confirm the mileage") == 1


def test_info_level_items_are_omitted_from_customer_view() -> None:
    """Info-level entries are filing tips for the operator — the customer email
    should stay minimal and not surface them as 'before you file' chores."""
    note = SimpleNamespace(
        filing_status="READY_TO_FILE",
        issues=[
            {"severity": "info", "code": "SURCHARGE_INCLUDED", "claim": "KY surcharge line included."},
        ],
        next_steps=[
            {"severity": "info", "code": "FYI", "recommended_action": "Reminder text."},
        ],
    )
    body = render_customer_view(sub=_sub(), ret=_ret(), note=note)
    assert "Before you file" not in body
    assert "SURCHARGE_INCLUDED" not in body
    assert "Reminder text." not in body


def test_string_items_are_skipped_not_dumped_as_code() -> None:
    """Older review notes used bare strings — never render them as bullets
    because they often contain code-style identifiers."""
    note = SimpleNamespace(
        filing_status="READY_TO_FILE",
        issues=["[warning] OLD_FORMAT: This should NOT appear in the customer email."],
        next_steps=[],
    )
    body = render_customer_view(sub=_sub(), ret=_ret(), note=note)
    assert "OLD_FORMAT" not in body


# ─── deterministic-fallback rendering (no agent) ─────────────────────────────


def test_fallback_uses_findings_when_no_agent_note() -> None:
    """When the AI agent fails, render_customer_view should still produce
    a useful customer email from the deterministic validator findings."""
    finding = SimpleNamespace(severity="warning", code="MPG_HIGH", message="Fleet MPG 14.27 is above 10.5 — likely missing fuel purchases.")
    body = render_customer_view(sub=_sub(), ret=_ret(mpg=14.27), findings=[finding])
    assert "Please review the attached files before filing." in body
    # The finding's plain message is what shows.
    assert "Fleet MPG 14.27 is above 10.5" in body
    # Still no dev markup.
    assert "MPG_HIGH" not in body


def test_fallback_filters_info_findings() -> None:
    info = SimpleNamespace(severity="info", code="OREGON_WMT", message="Oregon WMT note.")
    warn = SimpleNamespace(severity="warning", code="MPG_HIGH", message="MPG too high.")
    body = render_customer_view(sub=_sub(), ret=_ret(), findings=[info, warn])
    assert "MPG too high" in body
    assert "Oregon WMT note" not in body


# ─── rate-fallback prominent warning ─────────────────────────────────────────


def test_rate_warning_shown_prominently() -> None:
    """When current-quarter rates are unavailable, the customer must see a
    clear "don't file yet" notice, not just buried in the checklist."""
    body = render_customer_view(
        sub=_sub(),
        ret=_ret(warning="Q1-2026 rates unavailable — used Q4-2025 fallback. Don't file."),
        note=SimpleNamespace(filing_status="DO_NOT_FILE", issues=[], next_steps=[]),
    )
    assert "⚠️" in body or "Heads up" in body
    assert "rates unavailable" in body
    assert "We found issues" in body


# ─── full regression on dev-markup leakage ───────────────────────────────────


def test_no_dev_markup_leaks_under_realistic_agent_output() -> None:
    """Reproduces (a sanitized version of) the exact email that triggered this
    work, and guarantees the customer-facing render is clean."""
    note = SimpleNamespace(
        filing_status="READY_TO_FILE",
        issues=[
            {
                "severity": "warning",
                "code": "MILES_SUSPICIOUSLY_FEW",
                "claim": "Only 1 mileage row was parsed from file_miles.csv.",
                "evidence": {"source": "inspect_raw_inputs.findings[MILES_SUSPICIOUSLY_FEW]", "value": "mile_rows=1, total_miles=1000.0"},
                "filing_impact": "Missing mileage rows would understate taxable gallons.",
            },
            {
                "severity": "warning",
                "code": "BASE_JURISDICTION_MISSING",
                "claim": "No IFTA base jurisdiction is on file for this carrier.",
                "evidence": {"source": "client_context.base_jurisdiction", "value": "null"},
            },
        ],
        next_steps=[
            {
                "severity": "warning",
                "code": "CONFIRM_COMPLETE_MILEAGE",
                "recommended_action": "Verify that the mileage file contains all rows for Q1-2026.",
            },
        ],
    )
    body = render_customer_view(sub=_sub(), ret=_ret(), note=note, truck_count=1)
    # None of the dev artifacts visible to the customer.
    forbidden = [
        "MILES_SUSPICIOUSLY_FEW",
        "BASE_JURISDICTION_MISSING",
        "CONFIRM_COMPLETE_MILEAGE",
        "[warning]",
        "Evidence:",
        "filing_impact",
        '"source":',
        "{'source'",
        "READY_TO_FILE",  # raw status enum
    ]
    for tok in forbidden:
        assert tok not in body, f"developer markup leaked into customer email: {tok!r}"
