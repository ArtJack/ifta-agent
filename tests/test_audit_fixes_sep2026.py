"""Regression tests for the 2026-09-07 filing-gate audit.

The theme is one class of bug: **a return that understates the tax owed was
allowed through the filing gate as a warning.** IFTA returns go to government
portals, so "we noticed and mentioned it" is not an acceptable outcome for a
jurisdiction whose tax we computed as $0.00 because we had no rate for it.

Findings closed here:
  F-1  a jurisdiction with miles and a $0.00 rate filed as READY_WITH_WARNINGS
  F-2  an unparseable rate cell silently became $0.00 / a dropped jurisdiction
  F-4  a missing KY/VA surcharge line only warned
  F-9  a fail-open ``hasattr`` could drop surcharge tax entirely
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from ifta import rates as rates_module
from ifta.calc import compute_return
from ifta.models import CleanData, FuelRecord, MileageRecord
from ifta.rates import (
    FUEL_COLUMNS,
    RateMatrixInvalidError,
    RateTable,
    _parse_matrix,
    _strip_money,
    fetch_rates,
)
from ifta.review_packet import determine_filing_status
from ifta.validator import validate

DIESEL = FUEL_COLUMNS["diesel"]
RATES_DIR = Path(__file__).resolve().parent.parent / "data" / "rates"


def _table(rates: dict[str, float], surcharges: dict[str, float] | None = None) -> RateTable:
    return RateTable(
        quarter="Q3-2026",
        fuel="diesel",
        rates=rates,
        surcharge_rates=surcharges or {},
        requested_quarter="3Q2026",
        source_quarter="3Q2026",
        fallback_used=False,
        warning=None,
    )


def _data(miles: list[tuple[str, str, float]], fuel: list[tuple[str, str, float]]) -> CleanData:
    return CleanData(
        miles=[MileageRecord(t, s, m) for t, s, m in miles],
        fuel=[FuelRecord(t, s, g) for t, s, g in fuel],
    )


# --------------------------------------------------------------------------
# F-1 — a priced-out jurisdiction must block the filing, not warn about it
# --------------------------------------------------------------------------


def test_missing_rate_for_ifta_state_with_miles_blocks_filing() -> None:
    """AZ has miles but no rate in the matrix: its tax computes as $0.00.

    That is an understated return. It must not reach a portal.
    """
    data = _data([("T1", "CA", 1000), ("T1", "AZ", 500)], [("T1", "CA", 200)])
    ret = compute_return(data, _table({"CA": 0.971}))

    az = next(line for line in ret.lines if line.state == "AZ" and not line.is_surcharge)
    assert az.rate == 0
    assert az.tax_due == 0.0, "premise: a missing rate silently computes zero tax"

    findings = validate(data, ret)
    missing = [f for f in findings if f.code == "RATE_MISSING"]
    assert [f.state for f in missing] == ["AZ"]
    assert missing[0].severity == "error"

    gate = determine_filing_status(ret, findings)
    assert gate["status"] == "DO_NOT_FILE"
    assert any(r.startswith("[RATE_MISSING]") for r in gate["reasons"])


def test_oregon_zero_rate_does_not_block() -> None:
    """Oregon is deliberately rate-free (weight-mile tax). It must stay clean."""
    data = _data([("T1", "OR", 800)], [("T1", "OR", 100)])
    ret = compute_return(data, _table({"OR": 0.0}))

    findings = validate(data, ret)
    assert not [f for f in findings if f.code == "RATE_MISSING"]
    assert determine_filing_status(ret, findings)["status"] == "READY_TO_FILE"


@pytest.mark.parametrize("state", ["AK", "HI", "DC", "NT", "NU", "YT"])
def test_non_ifta_jurisdiction_warns_but_does_not_block(state: str) -> None:
    """The false positive that escalating RATE_MISSING could have introduced.

    Non-IFTA jurisdictions are legitimately absent from the rate matrix. Blocking
    on them would stop real filings during the season for no reason, so they must
    keep producing NON_IFTA_MILES (a warning) and never RATE_MISSING.
    """
    data = _data([("T1", "CA", 1000), ("T1", state, 200)], [("T1", "CA", 200)])
    ret = compute_return(data, _table({"CA": 0.971}))

    findings = validate(data, ret)
    assert not [f for f in findings if f.code == "RATE_MISSING"]
    assert [f.code for f in findings] == ["NON_IFTA_MILES"]
    assert determine_filing_status(ret, findings)["status"] == "READY_WITH_WARNINGS"


# --------------------------------------------------------------------------
# F-4 — a missing KY/VA surcharge line is a defective return, not a note
# --------------------------------------------------------------------------


def test_missing_surcharge_line_blocks_filing() -> None:
    data = _data([("T1", "KY", 1000)], [("T1", "KY", 150)])
    ret = compute_return(data, _table({"KY": 0.22}))  # no surcharge rate loaded

    findings = validate(data, ret)
    missing = [f for f in findings if f.code == "SURCHARGE_MISSING"]
    assert [f.state for f in missing] == ["KY"]
    assert missing[0].severity == "error"

    gate = determine_filing_status(ret, findings)
    assert gate["status"] == "DO_NOT_FILE"
    assert any(r.startswith("[SURCHARGE_MISSING]") for r in gate["reasons"])


def test_present_surcharge_line_is_info_and_files_clean() -> None:
    data = _data([("T1", "KY", 1000)], [("T1", "KY", 150)])
    ret = compute_return(data, _table({"KY": 0.22}, {"KY": 0.105}))

    findings = validate(data, ret)
    assert not [f for f in findings if f.code == "SURCHARGE_MISSING"]
    assert [f.severity for f in findings if f.code == "SURCHARGE_INCLUDED"] == ["info"]
    assert determine_filing_status(ret, findings)["status"] == "READY_TO_FILE"


def test_every_customer_path_blocks_via_the_shared_core(tmp_path, monkeypatch) -> None:
    """The gate must hold in ``compute_quarter``, which every customer path calls."""
    from ifta.quarter import compute_quarter

    inbox = tmp_path / "inbox" / "Q3-2026"
    inbox.mkdir(parents=True)
    (inbox / "miles.csv").write_text(
        "truck_id,state,miles\nT1,CA,6000\nT1,AZ,1000\n", encoding="utf-8"
    )
    (inbox / "fuel.csv").write_text("truck_id,state,gallons\nT1,CA,1000\n", encoding="utf-8")

    monkeypatch.setattr("ifta.quarter.fetch_rates", lambda *a, **k: _table({"CA": 0.971}))

    computed = compute_quarter(inbox, "Q3-2026")
    assert computed.blocked
    assert any("[RATE_MISSING]" in r for r in computed.block_reasons)


# --------------------------------------------------------------------------
# F-2 — an unparseable rate cell must be reported, never silently zeroed
# --------------------------------------------------------------------------


def test_strip_money_distinguishes_no_rate_from_garbage() -> None:
    assert _strip_money("$ 0.2200 ") == 0.22
    assert _strip_money("$1,234.5") == 1234.5
    # A legitimately absent rate (Oregon, Indiana surcharge) is zero...
    assert _strip_money("$-") == 0.0
    assert _strip_money("") == 0.0
    assert _strip_money("—") == 0.0
    # ...but anything we cannot read is *unknown*, which is not the same as zero.
    assert _strip_money("$ N/A") is None
    assert _strip_money("0.25*") is None
    assert _strip_money("see note") is None


def test_committed_rate_matrices_parse_clean() -> None:
    """Every rate file shipped in the repo must parse with nothing unreadable."""
    cached = sorted(RATES_DIR.glob("*.csv"))
    assert cached, "expected committed rate matrices"
    for path in cached:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
        rates, surcharges, unparseable = _parse_matrix(raw, DIESEL)
        assert unparseable == [], f"{path.name} has unreadable cells: {unparseable}"
        assert len(rates) >= 50, f"{path.name} parsed only {len(rates)} jurisdictions"
        assert set(surcharges) == {"KY", "VA"}, f"{path.name} surcharges: {sorted(surcharges)}"


def _matrix_with_broken_kentucky() -> str:
    raw = (RATES_DIR / "2Q2026.csv").read_text(encoding="utf-8-sig", errors="replace")
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        if line.upper().startswith("KENTUCKY") and "U.S." in line:
            cells = line.split(",")
            cells[2 + DIESEL] = "$ N/A"
            lines[i] = ",".join(cells)
            break
    else:  # pragma: no cover - guards the fixture, not the code
        pytest.fail("no KENTUCKY U.S. row found in 2Q2026.csv")
    return "\n".join(lines)


def test_unparseable_cell_is_reported_not_dropped() -> None:
    rates, _, unparseable = _parse_matrix(_matrix_with_broken_kentucky(), DIESEL)
    assert "KY" not in rates
    assert unparseable == ["KY"], "a jurisdiction we cannot price must be named, not omitted"


def test_download_with_unparseable_cell_falls_back_and_is_never_cached(
    tmp_path, monkeypatch
) -> None:
    cache = tmp_path / "rates"
    cache.mkdir()
    (cache / "2Q2026.csv").write_bytes((RATES_DIR / "2Q2026.csv").read_bytes())
    monkeypatch.setattr(rates_module, "CACHE_DIR", cache)
    monkeypatch.setattr(
        rates_module.requests,
        "get",
        lambda *a, **k: SimpleNamespace(
            content=_matrix_with_broken_kentucky().encode("utf-8"),
            raise_for_status=lambda: None,
        ),
    )

    table = fetch_rates("Q3-2026")

    assert table.fallback_used is True
    assert table.source_quarter == "2Q2026"
    assert not (cache / "3Q2026.csv").exists(), "a matrix we cannot fully read must not be cached"


def test_cached_matrix_with_unparseable_cell_is_rejected(tmp_path, monkeypatch) -> None:
    """The plausibility check must also guard the cache-read path.

    ``cache_path.exists()`` short-circuits every later fetch, so a matrix that
    was poisoned once would otherwise stay authoritative for the whole quarter.
    """
    cache = tmp_path / "rates"
    cache.mkdir()
    (cache / "3Q2026.csv").write_text(_matrix_with_broken_kentucky(), encoding="utf-8")
    monkeypatch.setattr(rates_module, "CACHE_DIR", cache)

    def _no_network(*a, **k):  # pragma: no cover - asserts we never get here
        raise AssertionError("cache hit must not trigger a download")

    monkeypatch.setattr(rates_module.requests, "get", _no_network)

    with pytest.raises(RateMatrixInvalidError, match="KY"):
        fetch_rates("Q3-2026")


def test_cached_matrix_below_plausibility_floor_is_rejected(tmp_path, monkeypatch) -> None:
    cache = tmp_path / "rates"
    cache.mkdir()
    truncated = (RATES_DIR / "2Q2026.csv").read_text(encoding="utf-8-sig").splitlines()[:40]
    (cache / "3Q2026.csv").write_text("\n".join(truncated), encoding="utf-8")
    monkeypatch.setattr(rates_module, "CACHE_DIR", cache)
    monkeypatch.setattr(
        rates_module.requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download")),
    )

    with pytest.raises(RateMatrixInvalidError, match="3Q2026"):
        fetch_rates("Q3-2026")


def test_rate_matrix_invalid_error_is_not_a_runtime_error() -> None:
    """``conftest.rates_or_skip`` skips on RuntimeError.

    If this became a RuntimeError, a corrupt committed matrix would silently
    skip the penny-accurate regressions instead of failing them.
    """
    assert not issubclass(RateMatrixInvalidError, RuntimeError)


# --------------------------------------------------------------------------
# F-9 — no fail-open on the surcharge lookup
# --------------------------------------------------------------------------


def test_compute_return_fails_closed_without_a_surcharge_lookup() -> None:
    """A rate table that cannot answer "is there a surcharge?" must not compute.

    Silently treating it as "no surcharge" drops a legally required tax line.
    """
    bogus = SimpleNamespace(
        quarter="Q3-2026",
        fuel="diesel",
        source_quarter="3Q2026",
        fallback_used=False,
        warning=None,
        get=lambda state, default=0.0: 0.22,
    )
    data = _data([("T1", "KY", 1000)], [("T1", "KY", 150)])
    with pytest.raises(AttributeError):
        compute_return(data, bogus)


# --------------------------------------------------------------------------
# Task 7 — the fallback warning must reach the logs, not just stdout
# --------------------------------------------------------------------------


def test_fallback_warning_goes_to_logger(tmp_path, monkeypatch, caplog, capsys) -> None:
    cache = tmp_path / "rates"
    cache.mkdir()
    (cache / "2Q2026.csv").write_bytes((RATES_DIR / "2Q2026.csv").read_bytes())
    monkeypatch.setattr(rates_module, "CACHE_DIR", cache)

    def _outage(*a, **k):
        raise requests.ConnectionError("iftach.org unreachable")

    monkeypatch.setattr(rates_module.requests, "get", _outage)

    with caplog.at_level(logging.WARNING, logger="ifta.rates"):
        table = fetch_rates("Q3-2026")

    assert table.fallback_used is True
    assert [r.name for r in caplog.records if "falling back to cached 2Q2026" in r.getMessage()]
    assert capsys.readouterr().out == "", "operational warnings belong in the log, not stdout"
