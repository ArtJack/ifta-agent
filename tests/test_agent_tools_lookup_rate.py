"""lookup_rate must return an explicit error for unknown jurisdictions.

RateTable.get() defaults to 0.0 for a missing code — indistinguishable from
Oregon's legitimate $0.00 weight-mile rate. The tool must therefore refuse to
answer for codes absent from the matrix instead of reporting a $0.00 rate the
review agent would repeat as fact.
"""

from __future__ import annotations

import json

import pytest

import ifta.agent.tools as tools
from ifta.rates import RateTable


@pytest.fixture
def fake_rates(monkeypatch: pytest.MonkeyPatch) -> RateTable:
    table = RateTable(
        quarter="2Q2026",
        fuel="diesel",
        rates={"CA": 1.036, "OR": 0.0},
        surcharge_rates={"KY": 0.102},
        requested_quarter="2Q2026",
        source_quarter="2Q2026",
    )
    monkeypatch.setattr(tools, "fetch_rates", lambda quarter, fuel="diesel": table)
    return table


def test_known_state_returns_rate(fake_rates: RateTable) -> None:
    out = json.loads(tools.lookup_rate("ca", "Q2-2026"))
    assert "error" not in out
    assert out["state"] == "CA"
    assert out["base_rate_usd_per_gallon"] == pytest.approx(1.036)


def test_oregon_zero_rate_is_not_an_error(fake_rates: RateTable) -> None:
    out = json.loads(tools.lookup_rate("OR", "Q2-2026"))
    assert "error" not in out
    assert out["base_rate_usd_per_gallon"] == 0.0
    assert out["total_effective_rate"] == 0.0


def test_unknown_state_is_explicit_error(fake_rates: RateTable) -> None:
    out = json.loads(tools.lookup_rate("ZZ", "Q2-2026"))
    assert "error" in out
    assert "ZZ" in out["error"]
    # The error payload must not carry a rate a model could misread as $0.
    assert "base_rate_usd_per_gallon" not in out
    assert "total_effective_rate" not in out
