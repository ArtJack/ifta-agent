"""Regression tests for the P3 cleanup batch shipped 2026-05-16.

D-007/008  quarter_key validates format upfront (no more 'inbox not found' for typos)
D-009      _display_path falls back to absolute when path is outside PROJECT_ROOT
D-010      malformed client.json raises ValueError with file path + line/col
D-012      ifta onboard warns when normalization drops characters
D-016      system prompt instructs the agent to verify data-shape vs profile
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from ifta.agent.prompts import SYSTEM_PROMPT
from ifta.cli import _display_path, main
from ifta.client import _context_from_metadata, quarter_key

# ---------------------------------------------------------------------------
# D-007 / D-008 — quarter format validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Q4-2025", "Q4-2025"),
        ("q4-2025", "Q4-2025"),
        ("Q4 2025", "Q4-2025"),
        ("Q4_2025", "Q4-2025"),
        ("4Q2025", "Q4-2025"),
        ("4q2025", "Q4-2025"),
        ("Q1-2026", "Q1-2026"),
        ("Q42025", "Q4-2025"),  # missing separator, still parseable
    ],
)
def test_quarter_key_accepts_valid_forms(raw: str, expected: str) -> None:
    assert quarter_key(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "ZZ-9999",
        "Q5-2025",  # only Q1-Q4
        "Q0-2025",
        "Q4-25",  # 2-digit year
        "Q4",  # missing year
        "not a quarter",
    ],
)
def test_quarter_key_rejects_invalid_forms(bad: str) -> None:
    with pytest.raises(ValueError):
        quarter_key(bad)


# ---------------------------------------------------------------------------
# D-009 — _display_path safe fallback
# ---------------------------------------------------------------------------


def test_display_path_inside_project_is_relative(tmp_path: Path) -> None:
    from ifta.cli import PROJECT_ROOT

    inside = PROJECT_ROOT / "outputs" / "test"
    assert _display_path(inside) == "outputs/test"


def test_display_path_outside_project_falls_back_to_absolute(tmp_path: Path) -> None:
    # tmp_path is in /var/folders/... — definitely not under PROJECT_ROOT
    outside = tmp_path / "qa-output"
    assert _display_path(outside) == str(outside)


def test_display_path_does_not_raise_for_arbitrary_path() -> None:
    """The whole point: never raise ValueError on display."""
    weird = Path("/some/random/absolute/path")
    _display_path(weird)  # would have raised in the old code


# ---------------------------------------------------------------------------
# D-010 — malformed client.json has good error message
# ---------------------------------------------------------------------------


def test_malformed_client_json_raises_with_path(tmp_path: Path) -> None:
    inbox = tmp_path / "Q1-2026"
    inbox.mkdir()
    (inbox / "client.json").write_text("{ not valid json", encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        _context_from_metadata(tmp_path, inbox)

    msg = str(excinfo.value)
    assert "client.json" in msg
    assert "line" in msg.lower() or "column" in msg.lower()
    assert str(inbox) in msg


def test_valid_client_json_returns_context(tmp_path: Path) -> None:
    inbox = tmp_path / "Q1-2026"
    inbox.mkdir()
    (inbox / "client.json").write_text(
        json.dumps({"client_id": "test_logistics", "name": "Test Logistics"}),
        encoding="utf-8",
    )
    ctx = _context_from_metadata(tmp_path, inbox)
    assert ctx is not None
    assert ctx.client_id == "test_logistics"


# ---------------------------------------------------------------------------
# D-012 — onboard warns on dropped characters
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_onboard_warns_when_chars_dropped(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Onboard with non-ASCII chars should warn the user."""
    import shutil

    from ifta.cli import PROJECT_ROOT

    # use a unique id that doesn't collide with the real registry,
    # and clean up after.
    test_id = "Café-Trucking-Ω"
    result = runner.invoke(main, ["onboard", test_id, "--name", "Cafe Co"])
    norm = "caf_trucking"
    try:
        assert result.exit_code == 0, result.output
        assert "Normalized" in result.output
        assert "dropped" in result.output.lower()
    finally:
        for p in [
            PROJECT_ROOT / "data" / "clients" / norm,
            PROJECT_ROOT / "inbox" / norm,
        ]:
            if p.exists():
                shutil.rmtree(p)


def test_onboard_does_not_warn_for_ascii(runner: CliRunner) -> None:
    """Plain ASCII ids shouldn't trigger the dropped-chars warning."""
    import shutil

    from ifta.cli import PROJECT_ROOT

    test_id = "abc_clean_carrier_test"
    result = runner.invoke(main, ["onboard", test_id])
    try:
        assert result.exit_code == 0, result.output
        assert "dropped" not in result.output.lower()
    finally:
        for p in [
            PROJECT_ROOT / "data" / "clients" / test_id,
            PROJECT_ROOT / "inbox" / test_id,
        ]:
            if p.exists():
                shutil.rmtree(p)


# ---------------------------------------------------------------------------
# D-016 — system prompt mentions data-shape sanity check
# ---------------------------------------------------------------------------


def test_system_prompt_includes_data_shape_check() -> None:
    """The new instruction must be present so the agent flags identity mismatches."""
    assert "Data-shape sanity check" in SYSTEM_PROMPT
    assert "fleet size" in SYSTEM_PROMPT
    assert "client-identity mismatch" in SYSTEM_PROMPT
    assert "DO NOT FILE" in SYSTEM_PROMPT


def test_system_prompt_includes_fuel_mileage_domain_knowledge() -> None:
    """The agent must reason about real trucking-data patterns, not alarm on them."""
    # Miles-without-fuel in a state is normal (full-tank range).
    assert "Miles in a state with NO fuel bought there is NORMAL" in SYSTEM_PROMPT
    # High MPG => missing fuel; low MPG => duplicate fuel.
    assert "MISSING fuel" in SYSTEM_PROMPT
    assert "DUPLICATE fuel" in SYSTEM_PROMPT
    # Cash receipts with no truck/card/driver are still valid evidence.
    assert "Cash fill-ups" in SYSTEM_PROMPT
    # Fuel-date gaps are expected, not errors.
    assert "fuel-date gap" in SYSTEM_PROMPT
