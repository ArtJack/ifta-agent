"""Regression tests for the 2026-08-01 architecture/bug review.

Each test pins a defect that could reach a customer: a wrong number on a tax
form, a stranded job, a packet that never arrives, or access granted to the
wrong person.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from ifta.client import load_registry, reload_registry
from ifta.ingest import _miles_unit_factor, parse_sheet
from ifta.web import worker as worker_module
from ifta.web.models import Submission

# ── ingest: km headers must convert ──────────────────────────────────────────


@pytest.mark.parametrize(
    "header",
    ["KM Driven", "Km Driven", "KM Total", "Total KM", "Distance (km)", "Kilometers"],
)
def test_km_headers_convert_to_miles(header: str) -> None:
    """Any km-labelled distance column converts.

    MILES_KEYWORDS classifies a header containing 'km' as distance, but the
    conversion used to require a trailing 'km' — so 'KM Driven' was read as
    miles and inflated distance by 1.609x on metric ELD exports.
    """
    assert _miles_unit_factor(header) == pytest.approx(0.621371)


@pytest.mark.parametrize("header", ["Miles", "Total Miles", "Miles (converted from KM)"])
def test_miles_headers_are_not_converted(header: str) -> None:
    assert _miles_unit_factor(header) == 1.0


def test_km_column_end_to_end_converts_rows() -> None:
    df = pd.DataFrame(
        [
            ["Truck", "State", "KM Driven"],
            ["101", "CA", "1000"],
        ]
    )
    miles, _fuel, _drivers, _cards = parse_sheet(df)
    assert len(miles) == 1
    assert miles[0].miles == pytest.approx(621.371, rel=1e-4)


# ── report: displayed gallons round the same way calc computed them ─────────


def test_portal_row_arithmetic_is_self_consistent(tmp_path: Path) -> None:
    """Taxable − Tax Paid must equal the printed Net on every portal row.

    calc rounds gallons half-up but the writers used builtin round() (banker's),
    so on an exact .5 boundary a row printed Taxable 803, Tax Paid 802, Net 0 —
    a filing artifact that fails its own arithmetic under audit.
    """
    import csv as _csv

    from ifta.calc import compute_return
    from ifta.models import CleanData, FuelRecord, MileageRecord
    from ifta.rates import RateTable
    from ifta.report import write_portal_csv

    data = CleanData(
        miles=[MileageRecord("t1", "CA", 8025.0)],
        fuel=[FuelRecord("t1", "CA", 802.5, 0.0)],
    )
    rates = RateTable(quarter="2Q2026", fuel="diesel", rates={"CA": 0.971}, surcharge_rates={})
    ret = compute_return(data, rates)

    out = tmp_path / "portal.csv"
    write_portal_csv(ret, out, portal="generic")
    rows = list(_csv.reader(out.open(encoding="utf-8")))

    header = next(r for r in rows if any("Taxable" in str(c) for c in r))
    hi = {name.strip(): i for i, name in enumerate(header)}
    tax_gal_i = next(i for name, i in hi.items() if name.startswith("Taxable Gal"))
    paid_i = next(i for name, i in hi.items() if name.startswith("Tax Paid Gal"))
    net_i = next(i for name, i in hi.items() if name.startswith("Net Taxable"))

    checked = 0
    for row in rows[rows.index(header) + 1 :]:
        if len(row) <= net_i or not str(row[tax_gal_i]).strip().lstrip("-").isdigit():
            continue
        taxable, paid, net = (int(row[tax_gal_i]), int(row[paid_i]), int(row[net_i]))
        assert taxable - paid == net, f"row arithmetic broken: {taxable} - {paid} != {net}"
        checked += 1
    assert checked, "expected at least one numeric jurisdiction row"


# ── customer packet: the deterministic gate blocks without the agent ─────────


def _ret(*, mpg: float = 6.5, rate_fallback_used: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        total_tax_due=100.0,
        fleet_mpg=mpg,
        fleet_miles=1000.0,
        fleet_gallons=1000.0 / mpg if mpg else 0.0,
        rate_warning=None,
        rate_fallback_used=rate_fallback_used,
    )


def test_effective_filing_status_uses_agent_note_when_present() -> None:
    from ifta.web.customer_view import effective_filing_status

    note = SimpleNamespace(filing_status="READY_TO_FILE")
    assert effective_filing_status(note, _ret(), []) == "READY_TO_FILE"


def test_effective_filing_status_falls_back_to_deterministic_gate() -> None:
    """note=None (agent skipped or failed) must not read as 'ready'."""
    from ifta.web.customer_view import effective_filing_status

    error = SimpleNamespace(severity="error", code="MPG_ZERO", message="No fuel parsed.")
    assert effective_filing_status(None, _ret(mpg=0.0), [error]) == "DO_NOT_FILE"
    assert effective_filing_status(None, _ret(), []) == "READY_TO_FILE"


# ── worker: orphaned jobs get reaped on the idle path, not only at startup ───


class _FakeDb:
    """Minimal stand-in for ifta.web.db with a scripted queue."""

    def __init__(self, jobs: list[Submission | None]) -> None:
        self._jobs = list(jobs)
        self.reap_calls = 0

    def describe_backend(self, path: object = None) -> str:
        return f"sqlite {path}"

    def claim_next_queued(self, _path: Path) -> Submission | None:
        return self._jobs.pop(0) if self._jobs else None

    def reap_stale_running(self, _path: Path, *, max_seconds_running: int) -> list:
        self.reap_calls += 1
        return []


def test_worker_reaps_on_idle_not_only_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A startup-only reap can never recover the realistic crash.

    launchd KeepAlive and Azure Container Apps restart a dead worker within
    seconds, so at startup the orphaned RUNNING row is far younger than the
    stale cutoff and is skipped — and nothing ever looked again. The customer's
    job sat in RUNNING forever with no failure email.
    """
    fake = _FakeDb([None])  # queue empty → straight to the idle path
    monkeypatch.setattr(worker_module, "db", fake)

    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        raise KeyboardInterrupt  # exit run_forever after one idle pass

    monkeypatch.setattr(worker_module.time, "sleep", fake_sleep)

    worker_module.run_forever(Path("/tmp/db"), Path("/tmp/subs"), poll_interval_seconds=1.0)

    assert fake.reap_calls == 2, "expected a startup reap AND an idle-path reap"
    assert slept == [1.0]


def test_worker_survives_a_transient_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DB blip must not kill the loop — that is what strands RUNNING rows."""
    calls = {"n": 0}

    class _FlakyDb(_FakeDb):
        def claim_next_queued(self, _path: Path) -> Submission | None:
            calls["n"] += 1
            raise OSError("connection reset by peer")

    fake = _FlakyDb([])
    monkeypatch.setattr(worker_module, "db", fake)

    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) >= 2:
            raise KeyboardInterrupt
        return None

    monkeypatch.setattr(worker_module.time, "sleep", fake_sleep)
    # Must return normally (via KeyboardInterrupt), not propagate the OSError.
    worker_module.run_forever(Path("/tmp/db"), Path("/tmp/subs"), poll_interval_seconds=1.0)
    assert calls["n"] >= 2, "worker should have retried after the DB error"


# ── client registry: one bad client.json must not silence the bot ────────────


def test_malformed_client_json_is_skipped_not_fatal(tmp_path: Path) -> None:
    """load_registry runs before nearly every Telegram reply. An uncaught
    parse error there made the bot stop answering everyone, silently."""
    registry = tmp_path / "data" / "clients"
    good = registry / "good_co"
    good.mkdir(parents=True)
    (good / "client.json").write_text(
        json.dumps({"client_id": "good_co", "name": "Good Co", "active": True}),
        encoding="utf-8",
    )
    bad = registry / "bad_co"
    bad.mkdir(parents=True)
    (bad / "client.json").write_text('{"client_id": "bad_co",}', encoding="utf-8")  # trailing comma

    reload_registry(tmp_path)
    try:
        records = load_registry(tmp_path)
        assert "good_co" in records
        assert "bad_co" not in records
    finally:
        reload_registry(tmp_path)


def test_non_numeric_telegram_id_skips_only_that_client(tmp_path: Path) -> None:
    registry = tmp_path / "data" / "clients"
    bad = registry / "bad_ids"
    bad.mkdir(parents=True)
    (bad / "client.json").write_text(
        json.dumps({"client_id": "bad_ids", "telegram_user_ids": ["not-a-number"]}),
        encoding="utf-8",
    )
    reload_registry(tmp_path)
    try:
        assert load_registry(tmp_path) == {}
    finally:
        reload_registry(tmp_path)


# ── telegram access file: atomic writes, corrupt file never silently wiped ───


def test_access_file_write_is_atomic_and_leaves_no_temp(tmp_path: Path) -> None:
    from ifta.telegram_bot import _write_raw_access_file, telegram_access_path

    _write_raw_access_file(tmp_path, {"clients": {"acme": [1, 2]}})
    path = telegram_access_path(tmp_path)
    assert json.loads(path.read_text())["clients"] == {"acme": [1, 2]}
    assert not list(path.parent.glob("*.tmp")), "temp file should be renamed, not left behind"


def test_corrupt_access_file_refuses_to_be_overwritten(tmp_path: Path) -> None:
    """Returning {} for a corrupt file made the next DM erase every approval."""
    from ifta.telegram_bot import (
        AccessFileCorruptError,
        _read_raw_access_file,
        telegram_access_path,
    )

    path = telegram_access_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"clients": {"acme": [1,2]},', encoding="utf-8")  # truncated

    with pytest.raises(AccessFileCorruptError):
        _read_raw_access_file(tmp_path)


def test_load_telegram_access_fails_closed_on_corruption(tmp_path: Path) -> None:
    """The read-only authorization path denies everyone rather than crashing."""
    from ifta.telegram_bot import load_telegram_access, telegram_access_path

    path = telegram_access_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("}{not json", encoding="utf-8")
    assert load_telegram_access(tmp_path) == {}


# ── rates: outages degrade to a cached quarter instead of crashing ───────────


def test_network_outage_falls_back_to_cached_quarter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a 404 used to trigger the fallback walk. An unreachable iftach.org
    crashed the whole submission even with a good cached prior quarter."""
    import ifta.rates as rates_module

    cache = tmp_path / "rates"
    cache.mkdir()
    real_2q = Path(rates_module.CACHE_DIR) / "2Q2026.csv"
    if not real_2q.exists():
        pytest.skip("2Q2026 rate matrix not cached in this checkout")
    (cache / "2Q2026.csv").write_bytes(real_2q.read_bytes())
    monkeypatch.setattr(rates_module, "CACHE_DIR", cache)

    def boom(*_a: object, **_k: object) -> None:
        raise requests.ConnectionError("iftach.org unreachable")

    monkeypatch.setattr(rates_module.requests, "get", boom)

    table = rates_module.fetch_rates("Q3-2026")
    assert table.fallback_used is True
    assert table.source_quarter == "2Q2026"
    assert table.warning and "Do not file" in table.warning
    assert len(table.rates) > 50


def test_junk_response_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A WAF/maintenance HTML page parses to ~0 rates. Caching it would poison
    the quarter permanently and compute $0 tax for every jurisdiction."""
    import ifta.rates as rates_module

    cache = tmp_path / "rates"
    cache.mkdir()
    monkeypatch.setattr(rates_module, "CACHE_DIR", cache)

    class _Resp:
        content = b"<html><body>Site under maintenance</body></html>"

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(rates_module.requests, "get", lambda *_a, **_k: _Resp())

    with pytest.raises(RuntimeError, match="No IFTA rate matrix available"):
        rates_module.fetch_rates("Q3-2026")
    assert not list(cache.glob("*.csv")), "junk response must not be cached"
