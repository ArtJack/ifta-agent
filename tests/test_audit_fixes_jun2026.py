"""Regression tests for the 2026-06-24 full-project bug-audit pass.

Each test pins one verified-real defect from the audit so it can't silently
regress. Grouped by the source bug; see the audit notes for context.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pandas as pd
import pytest

from ifta.calc import _round_half_up_int, compute_return
from ifta.ingest import _to_float, parse_sheet
from ifta.models import (
    IFTA_JURISDICTIONS,
    NON_IFTA,
    CleanData,
    FuelRecord,
    MileageRecord,
)
from ifta.rates import RateTable

# ---------------------------------------------------------------------------
# Bug: NON_IFTA omitted the three Canadian territories (YT/NT/NU)
# ---------------------------------------------------------------------------


def test_canadian_territories_are_non_ifta() -> None:
    for code in ("YT", "NT", "NU"):
        assert code in NON_IFTA, f"{code} should be non-IFTA"
        assert code not in IFTA_JURISDICTIONS, f"{code} must not be a taxable jurisdiction"


def test_models_non_ifta_matches_regulations_kb() -> None:
    """models.NON_IFTA must stay in sync with the regulations KB list."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    kb = json.loads((root / "data" / "regulations.json").read_text(encoding="utf-8"))
    kb_list = set(kb["special_states"]["non_ifta_jurisdictions"]["list"])
    assert kb_list == set(NON_IFTA)


def test_territory_miles_are_not_taxed() -> None:
    """A truck running in Yukon must produce a 0-tax line, not a taxable one."""
    data = CleanData(
        miles=[MileageRecord("T1", "TX", 1000.0), MileageRecord("T1", "YT", 500.0)],
        fuel=[FuelRecord("T1", "TX", 150.0, 30.0)],
    )
    rates = RateTable(
        quarter="Q2-2026",
        fuel="Diesel",
        rates={"TX": 0.20, "YT": 0.50},  # even if a YT rate exists, it must not apply
        surcharge_rates={},
    )
    ret = compute_return(data, rates)
    yt = next(ln for ln in ret.lines if ln.state == "YT")
    assert yt.rate == 0.0
    assert yt.tax_due == 0.0


# ---------------------------------------------------------------------------
# Bug: gallon rounding used builtin round() (banker's) instead of half-up
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Bug (CRITICAL): truck-first side-by-side blocks misattributed miles/fuel
# ---------------------------------------------------------------------------


def test_truck_first_side_by_side_blocks_attribute_to_correct_truck() -> None:
    """Two trucks laid out side-by-side, each block starting with its own Truck
    column, must keep each truck's miles and gallons with that truck."""
    df = pd.DataFrame(
        [
            ["Truck", "State", "Miles", "Gallons", "Truck", "State", "Miles", "Gallons"],
            ["T1", "CA", "100", "15", "T2", "CA", "200", "30"],
            ["T1", "TX", "50", "8", "T2", "NV", "75", "11"],
        ]
    )
    miles, fuel, _drivers, _cards = parse_sheet(df)

    miles_by = {(m.truck_id, m.state): m.miles for m in miles}
    assert miles_by == {
        ("T1", "CA"): 100.0,
        ("T1", "TX"): 50.0,
        ("T2", "CA"): 200.0,
        ("T2", "NV"): 75.0,
    }
    fuel_by = {(f.truck_id, f.state): f.gallons for f in fuel}
    assert fuel_by == {
        ("T1", "CA"): 15.0,
        ("T1", "TX"): 8.0,
        ("T2", "CA"): 30.0,
        ("T2", "NV"): 11.0,
    }


def test_state_first_side_by_side_blocks_still_work() -> None:
    """The pre-existing year-hint/state-first layout must keep working."""
    df = pd.DataFrame(
        [
            ["2013", "State", "Miles", "2014", "State", "Miles"],
            ["", "CA", "100", "", "CA", "200"],
            ["", "TX", "50", "", "NV", "75"],
        ]
    )
    miles, _fuel, _drivers, _cards = parse_sheet(df)
    miles_by = {(m.truck_id, m.state): m.miles for m in miles}
    assert miles_by == {
        ("2013", "CA"): 100.0,
        ("2013", "TX"): 50.0,
        ("2014", "CA"): 200.0,
        ("2014", "NV"): 75.0,
    }


@pytest.mark.parametrize(
    "value,expected",
    [(0.5, 1), (1.5, 2), (2.5, 3), (3.5, 4), (2.4, 2), (2.6, 3), (-0.5, -1)],
)
def test_round_half_up_int_breaks_ties_upward(value: float, expected: int) -> None:
    assert _round_half_up_int(value) == expected
    # Differs from banker's rounding on exact-half ties.
    if value in (0.5, 2.5):
        assert _round_half_up_int(value) != round(value)


# ---------------------------------------------------------------------------
# Bug: km treated as miles, liters unrecognized, decimal-comma mis-scaled
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1,234.56", 1234.56),  # US: comma thousands, dot decimal
        ("1.234,56", 1234.56),  # EU: dot thousands, comma decimal
        ("1234,56", 1234.56),  # EU decimal comma (was mis-scaled 100x)
        ("12,5", 12.5),  # decimal comma, short fraction (was 125)
        ("1,234", 1234.0),  # ambiguous → keep US thousands reading
        ("(100)", -100.0),  # accounting negative
        ("$2,000.00", 2000.0),
        ("", 0.0),
        ("nan", 0.0),
        ("100", 100.0),
    ],
)
def test_to_float_handles_us_and_eu_numbers(raw: str, expected: float) -> None:
    assert _to_float(raw) == pytest.approx(expected)


def test_km_distance_column_converts_to_miles() -> None:
    df = pd.DataFrame(
        [
            ["Truck", "State", "Distance (km)", "Gallons"],
            ["T1", "CA", "100", "10"],
        ]
    )
    miles, fuel, _d, _c = parse_sheet(df)
    assert miles[0].miles == pytest.approx(62.1371)  # 100 km → miles
    assert fuel[0].gallons == pytest.approx(10.0)  # gallons untouched


def test_liters_column_converts_to_gallons() -> None:
    df = pd.DataFrame(
        [
            ["Truck", "State", "Miles", "Liters"],
            ["T1", "CA", "100", "100"],
        ]
    )
    miles, fuel, _d, _c = parse_sheet(df)
    assert miles[0].miles == pytest.approx(100.0)  # miles untouched
    assert fuel[0].gallons == pytest.approx(26.4172, rel=1e-4)  # 100 L → gal


def test_plain_miles_and_gallons_are_not_converted() -> None:
    df = pd.DataFrame(
        [
            ["Truck", "State", "Miles", "Gallons"],
            ["T1", "CA", "100", "15"],
        ]
    )
    miles, fuel, _d, _c = parse_sheet(df)
    assert miles[0].miles == 100.0
    assert fuel[0].gallons == 15.0


def test_taxable_gallons_round_half_up_at_boundary() -> None:
    """Miles/MPG landing on an exact .5 gallon must round up, matching dollars."""
    # fleet_mpg = 10.0; 25 miles → 2.5 taxable gallons → must be 3 (not banker's 2).
    data = CleanData(
        miles=[MileageRecord("T1", "TX", 25.0)],
        fuel=[FuelRecord("T1", "TX", 2.5, 0.0)],
    )
    rates = RateTable(quarter="Q2-2026", fuel="Diesel", rates={"TX": 0.20}, surcharge_rates={})
    ret = compute_return(data, rates)
    assert ret.fleet_mpg == 10.0
    tx = next(ln for ln in ret.lines if ln.state == "TX")
    assert tx.taxable_gal == 3.0  # 2.5 rounded half-up
    # net = 3 - round_half_up(2.5)=3 → 0; tax_due 0 but rounding path exercised.
    assert tx.tax_due == Decimal("0").quantize(Decimal("0.01"), ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Bug: _parse_date rejected Excel 'YYYY-MM-DD 00:00:00' → silent double-count
# ---------------------------------------------------------------------------


def test_parse_date_accepts_excel_datetime_cells() -> None:
    from datetime import date as _date

    from ifta.intake.receipts import _parse_date

    assert _parse_date("2025-10-15 00:00:00") == _date(2025, 10, 15)
    assert _parse_date("2025-10-15T13:45:00") == _date(2025, 10, 15)
    assert _parse_date("10/15/2025") == _date(2025, 10, 15)
    assert _parse_date("2025-10-15") == _date(2025, 10, 15)
    assert _parse_date("not a date") is None


def test_reconcile_reuses_the_same_parse_date() -> None:
    """reconcile must share receipts._parse_date so the fix can't drift apart."""
    from datetime import date as _date

    from ifta.intake import reconcile
    from ifta.intake.receipts import _parse_date

    assert reconcile._parse_date is _parse_date
    assert reconcile._parse_date("2025-10-15 00:00:00") == _date(2025, 10, 15)


def test_excel_datetime_receipt_is_detected_as_duplicate() -> None:
    """A receipt dated from Excel ('… 00:00:00') must still match an existing
    transaction so it is not silently counted twice."""
    from ifta.intake.receipts import (
        ExistingFuelTransaction,
        ReceiptCandidate,
        find_duplicate,
    )

    candidate = ReceiptCandidate(
        source_file="receipt.jpg",
        date="2025-10-15 00:00:00",
        state="CA",
        gallons=50.0,
        amount=200.0,
        vendor="Pilot",
    )
    existing = [
        ExistingFuelTransaction(
            date="2025-10-15", state="CA", gallons=50.0, amount=200.0, vendor="Pilot"
        )
    ]
    assert find_duplicate(candidate, existing) is not None


# ---------------------------------------------------------------------------
# Bug: tool_runner had no max_iterations → unbounded agent loop/cost
# ---------------------------------------------------------------------------


def test_run_agent_passes_finite_max_iterations(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from ifta.agent import runner

    captured: dict = {}

    def fake_tool_runner(**kwargs):
        captured.update(kwargs)
        msg = SimpleNamespace(
            role="assistant",
            content=[SimpleNamespace(type="text", text="ok")],
            usage=None,
        )
        return iter([msg])

    fake_client = SimpleNamespace(
        beta=SimpleNamespace(messages=SimpleNamespace(tool_runner=fake_tool_runner))
    )
    monkeypatch.setattr(runner, "_client", lambda: fake_client)

    text, _, _ = runner.run_agent("hi", model="claude-haiku-4-5")
    assert text == "ok"
    assert captured["max_iterations"] == runner.DEFAULT_MAX_ITERATIONS
    assert isinstance(captured["max_iterations"], int) and captured["max_iterations"] > 0


# ---------------------------------------------------------------------------
# Bug: eval total_tax_due fallback used substring match → false-green
# ---------------------------------------------------------------------------


def test_mentions_amount_requires_whole_token() -> None:
    from ifta.eval.runner import _mentions_amount

    assert _mentions_amount("Total tax due: $795.16", 795.16)
    assert _mentions_amount("owed 795.16 total", 795.16)
    assert _mentions_amount("paid $1,795.16? no, 795.16", 795.16)
    # False-greens the old substring check would have let through:
    assert not _mentions_amount("fleet ran 7950 miles", 795)
    assert not _mentions_amount("subtotal was 1795.16", 795)
    assert not _mentions_amount("amount 795.16", 795)  # 795 != 795.16


def test_grade_total_tax_due_has_no_substring_false_green() -> None:
    from ifta.agent.runner import ReviewNote
    from ifta.eval.runner import grade_assertions

    note = ReviewNote(
        summary="Fleet drove 7950 miles.", issues=[], filing_reminders=[], next_steps=[]
    )
    results = grade_assertions(
        {"total_tax_due": 795}, response_text="Fleet drove 7950 miles.", note=note
    )
    r = next(x for x in results if x.name.startswith("total_tax_due"))
    assert not r.passed


def test_grade_total_tax_due_matches_real_total() -> None:
    from ifta.agent.runner import ReviewNote
    from ifta.eval.runner import grade_assertions

    note = ReviewNote(
        summary="Total tax due is $795.16.", issues=[], filing_reminders=[], next_steps=[]
    )
    results = grade_assertions(
        {"total_tax_due": 795.16}, response_text="Total tax due is $795.16.", note=note
    )
    r = next(x for x in results if x.name.startswith("total_tax_due"))
    assert r.passed


# ---------------------------------------------------------------------------
# Bug: scaffold_client overwrote a partial client dir (profile/history, no
#      client.json)
# ---------------------------------------------------------------------------


def test_scaffold_refuses_partial_client_dir(tmp_path) -> None:
    from ifta.client import ScaffoldError, scaffold_client

    cdir = tmp_path / "data" / "clients" / "acme"
    cdir.mkdir(parents=True)
    # A partially set-up client: profile present, but no client.json yet.
    (cdir / "profile.json").write_text('{"keep": "this"}', encoding="utf-8")

    with pytest.raises(ScaffoldError, match=r"profile\.json"):
        scaffold_client(tmp_path, "acme", make_inbox=False)

    # The pre-existing profile must be left untouched.
    assert (cdir / "profile.json").read_text(encoding="utf-8") == '{"keep": "this"}'


def test_scaffold_creates_fresh_client(tmp_path) -> None:
    from ifta.client import scaffold_client

    result = scaffold_client(tmp_path, "acme", name="ACME LLC", make_inbox=False)
    cdir = tmp_path / "data" / "clients" / "acme"
    assert (cdir / "client.json").exists()
    assert (cdir / "profile.json").exists()
    assert result.client_id == "acme"


# ---------------------------------------------------------------------------
# Bug: notify._truncate sliced assembled HTML → broke <pre>/entities (TG 400)
# ---------------------------------------------------------------------------


def test_truncate_recloses_open_pre_tag() -> None:
    from ifta import notify

    body = "<b>Header</b>\n\n<pre>" + ("data line &amp; more\n" * 200) + "</pre>"
    out = notify._truncate(body, 200)
    assert len(out) <= 200
    assert out.count("<pre>") == out.count("</pre>")  # <pre> balanced
    assert out.count("<b>") == out.count("</b>")  # <b> balanced
    assert out.endswith("</pre>")  # cut fell inside <pre>, so it was reclosed
    assert "…(truncated)" in out


def test_truncate_never_splits_an_entity() -> None:
    import re as _re

    from ifta import notify

    body = "<pre>" + ("&amp;" * 100) + "</pre>"
    out = notify._truncate(body, 40)
    assert len(out) <= 40
    # No dangling partial entity: stripping whole entities must leave no bare '&'.
    leftover = _re.sub(r"&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);", "", out)
    assert "&" not in leftover
    assert out.count("<pre>") == out.count("</pre>")


def test_truncate_leaves_short_html_untouched() -> None:
    from ifta import notify

    body = "<b>ok</b>"
    assert notify._truncate(body, 100) == body
