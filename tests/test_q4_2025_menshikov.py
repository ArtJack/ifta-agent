"""Validate against MENSHIKOV LLC Q4 2025 CDTFA filing — should match $795.16."""

from pathlib import Path

import pytest

from ifta.calc import compute_return
from ifta.ingest import ingest_folder
from ifta.rates import fetch_rates

ROOT = Path(__file__).resolve().parents[1]
INBOX = ROOT / "inbox" / "Q4-2025"


@pytest.mark.skipif(
    not INBOX.exists(),
    reason="inbox/Q4-2025 holds private client data (gitignored); absent on clean checkouts",
)
def test_q4_2025_menshikov() -> None:
    data = ingest_folder(ROOT / "inbox" / "Q4-2025")
    rates = fetch_rates("Q4-2025")
    ret = compute_return(data, rates)
    # CDTFA filing 0-054-520-028: total $795.16, fleet MPG 7.31
    assert round(ret.fleet_miles) == 29_946, ret.fleet_miles
    assert round(ret.fleet_mpg, 2) == 7.31, ret.fleet_mpg
    assert round(ret.total_tax_due, 2) == 795.16, ret.total_tax_due

    # Surcharge lines exist for KY and VA but not IN
    sur = {ln.state for ln in ret.lines if ln.is_surcharge}
    assert sur == {"KY", "VA"}, sur


if __name__ == "__main__":
    test_q4_2025_menshikov()
    print("OK — Q4 2025 matches CDTFA filing $795.16")
