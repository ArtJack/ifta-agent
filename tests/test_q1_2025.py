"""Validate the pipeline reproduces the Q1 2025 historical totals.

Real-data regression — the Q1-2025 inbox is untracked PII, so this is skipped on
a clean checkout. See `test_q2_2026_synthetic.py` for the hermetic equivalent.
"""

from pathlib import Path

import pytest

from ifta.calc import compute_return
from ifta.ingest import ingest_folder
from ifta.rates import fetch_rates

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "inbox" / "Q1-2025" / "q1_2025_miles_and_fuel.xlsx"

pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(),
    reason="real Q1-2025 data is untracked PII; present only on the owner's machine",
)


def test_q1_2025_totals() -> None:
    data = ingest_folder(ROOT / "inbox" / "Q1-2025")
    rates = fetch_rates("Q1-2025")
    ret = compute_return(data, rates)
    # original xlsx: 110,525 miles / 17,137 gallons / MPG ≈ 6.45
    assert round(ret.fleet_miles) == 110_525, ret.fleet_miles
    assert round(ret.fleet_gallons) == 17_137, ret.fleet_gallons
    assert round(ret.fleet_mpg, 2) == 6.45, ret.fleet_mpg


if __name__ == "__main__":
    test_q1_2025_totals()
    print("OK")
