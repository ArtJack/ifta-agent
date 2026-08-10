"""Hermetic end-to-end calc regression against committed synthetic data.

TEST LOGISTICS LLC, Q2-2026, is fully synthetic (scripts/generate_test_data.py,
fixed seed), committed under inbox/Q2-2026 alongside a tracked 2Q2026 rate
matrix. So these golden numbers reproduce on a clean checkout with no network —
unlike the real-carrier regressions (test_q4_2025_menshikov, test_q1_2025) which
skip when their untracked PII inputs are absent.
"""

from pathlib import Path

from ifta.calc import compute_return
from ifta.ingest import ingest_folder
from ifta.rates import fetch_rates

ROOT = Path(__file__).resolve().parents[1]


def _compute():
    data = ingest_folder(ROOT / "inbox" / "Q2-2026")
    rates = fetch_rates("Q2-2026")
    return compute_return(data, rates)


def test_q2_2026_synthetic_golden_totals() -> None:
    ret = _compute()
    assert round(ret.fleet_miles) == 120_466, ret.fleet_miles
    assert round(ret.fleet_gallons) == 17_649, ret.fleet_gallons
    assert round(ret.fleet_mpg, 2) == 6.83, ret.fleet_mpg
    assert round(ret.total_tax_due, 2) == 265.60, ret.total_tax_due


def test_q2_2026_rates_resolve_offline_without_fallback() -> None:
    """The 2Q2026 rate matrix is committed, so no network fetch / fallback."""
    ret = _compute()
    assert ret.rate_source_quarter == "2Q2026"
    assert ret.rate_fallback_used is False


def test_q2_2026_has_five_trucks_and_ky_va_surcharges() -> None:
    ret = _compute()
    assert sorted(t.truck_id for t in ret.trucks) == ["T1", "T2", "T3", "T4", "T5"]
    sur = {ln.state for ln in ret.lines if ln.is_surcharge}
    assert sur == {"KY", "VA"}, sur
