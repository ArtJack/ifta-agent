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

from ifta.web.customer_view import render_customer_summary, render_customer_view
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


def _ret(
    *,
    tax: float = 15.75,
    mpg: float = 6.67,
    miles: float = 1000.0,
    gallons: float = 150.0,
    warning: str | None = None,
) -> SimpleNamespace:
    """Stand-in for IftaReturn — we only read the few attributes used here."""
    return SimpleNamespace(
        total_tax_due=tax,
        fleet_mpg=mpg,
        fleet_miles=miles,
        fleet_gallons=gallons,
        rate_warning=warning,
    )


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


# ─── customer summary report ─────────────────────────────────────────────────


def test_summary_basic_shape() -> None:
    note = SimpleNamespace(
        filing_status="READY_TO_FILE", issues=[], filing_reminders=[], next_steps=[]
    )
    body = render_customer_summary(
        sub=_sub(),
        ret=_ret(),
        note=note,
        truck_count=2,
        attached_files=["ifta_portal.csv", "summary_report.md", "trucks/T1.xlsx"],
    )
    # Header carries quarter + carrier.
    assert "# IFTA Q1-2026 Summary Report — ACME LOGISTICS" in body
    # Plain-English status header (no raw enum).
    assert "Status: Ready to file" in body
    assert "READY_TO_FILE" not in body
    # Key numbers section.
    assert "Total tax due:** $15.75" in body
    assert "Fleet MPG:** 6.67" in body
    assert "Trucks on this return:** 2" in body
    # Attachments listed.
    assert "ifta_portal.csv" in body
    assert "summary_report.md" in body
    # No "Things to double-check" when there are no warnings.
    assert "Things to double-check before filing" not in body


def test_summary_problems_have_headline_why_what_sections() -> None:
    note = SimpleNamespace(
        filing_status="READY_WITH_WARNINGS",
        issues=[
            {
                "severity": "warning",
                "code": "MILES_SUSPICIOUSLY_FEW",
                "claim": "Only 1 mileage row was parsed from file_miles.csv. A typical quarter has dozens to hundreds of rows.",
                "filing_impact": "Missing mileage rows would understate taxable gallons and the filing could be incorrect.",
                "recommended_action": "Verify the file you uploaded contains all Q1-2026 mileage.",
            },
        ],
        filing_reminders=[],
        next_steps=[],
    )
    body = render_customer_summary(sub=_sub(), ret=_ret(), note=note, truck_count=1)
    assert "## Things to double-check before filing" in body
    assert "### Only 1 mileage row was parsed from file_miles.csv." in body  # headline
    assert "A typical quarter has dozens to hundreds of rows." in body  # detail
    assert "**Why it matters:** Missing mileage rows would understate" in body
    assert "**What to do:** Verify the file" in body
    # Critical: no developer markup.
    for forbidden in ["MILES_SUSPICIOUSLY_FEW", "[warning]", "Evidence:", "filing_impact"]:
        assert forbidden not in body, f"summary leaked dev markup: {forbidden!r}"


def test_summary_pairs_next_step_action_with_issue() -> None:
    """When a next_step recommended_action matches an issue's claim, the
    summary surfaces it as that issue's 'What to do' — not as a duplicate
    standalone item."""
    note = SimpleNamespace(
        filing_status="READY_WITH_WARNINGS",
        issues=[
            {
                "severity": "warning",
                "code": "X",
                "claim": "Mileage looks light for the quarter.",
                "filing_impact": "Understates taxable gallons.",
            },
        ],
        next_steps=[
            {
                "severity": "warning",
                "code": "X",
                "claim": "Mileage looks light for the quarter.",
                "recommended_action": "Re-upload the full quarter's mileage.",
            },
        ],
        filing_reminders=[],
    )
    body = render_customer_summary(sub=_sub(), ret=_ret(), note=note)
    assert body.count("### Mileage looks light") == 1
    assert "**What to do:** Re-upload the full quarter's mileage" in body


def test_summary_does_not_double_emit_paired_next_step() -> None:
    """Regression: an action paired into an issue's 'What to do' must not also
    appear as a standalone empty problem entry from the next_steps pass."""
    note = SimpleNamespace(
        filing_status="READY_WITH_WARNINGS",
        issues=[
            {
                "severity": "warning",
                "code": "BASE",
                "claim": "No IFTA base jurisdiction is on file.",
                "filing_impact": "Wrong portal could reject the filing.",
            },
        ],
        next_steps=[
            {
                "severity": "warning",
                "code": "BASE",
                "claim": "No IFTA base jurisdiction is on file.",
                "recommended_action": "Reply with your IFTA base state so we file with the right portal.",
            },
        ],
        filing_reminders=[],
    )
    body = render_customer_summary(sub=_sub(), ret=_ret(), note=note)
    # The action shows once, paired with the issue — never as its own section.
    assert body.count("Reply with your IFTA base state") == 1
    assert body.count("### Reply with your IFTA base state") == 0


def test_summary_renders_filing_reminders_as_tips() -> None:
    note = SimpleNamespace(
        filing_status="READY_TO_FILE",
        issues=[],
        filing_reminders=[
            {
                "severity": "info",
                "code": "SURCHARGE_INCLUDED",
                "claim": "Kentucky surcharge line is included — enter it as a separate line on the portal.",
            },
        ],
        next_steps=[],
    )
    body = render_customer_summary(sub=_sub(), ret=_ret(), note=note)
    assert "## Filing tips" in body
    assert "Kentucky surcharge line is included" in body
    assert "SURCHARGE_INCLUDED" not in body


def test_summary_falls_back_to_findings_when_no_agent() -> None:
    f1 = SimpleNamespace(
        severity="warning",
        code="MPG_HIGH",
        message="Fleet MPG 14.27 is above 10.5 — likely missing fuel purchases.",
    )
    body = render_customer_summary(sub=_sub(), ret=_ret(mpg=14.27), findings=[f1])
    assert "Things to double-check" in body
    assert "Fleet MPG 14.27 is above 10.5" in body
    assert "MPG_HIGH" not in body


def test_summary_shows_rate_warning_prominently() -> None:
    body = render_customer_summary(
        sub=_sub(),
        ret=_ret(warning="Q1-2026 rates unavailable — Q4-2025 used. Do not file."),
        note=SimpleNamespace(filing_status="DO_NOT_FILE", issues=[], filing_reminders=[], next_steps=[]),
    )
    assert "⚠️ Rate notice" in body
    assert "rates unavailable" in body
    assert "Do NOT file yet" in body
