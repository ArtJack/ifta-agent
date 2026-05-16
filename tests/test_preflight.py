"""Pure tests for the preflight raw-input checker.

Builds temp-dir inboxes representing happy + sad scenarios, runs
`preflight_inputs`, and checks the produced findings.
"""

from pathlib import Path
from typing import Any

import pandas as pd

from ifta.preflight import PreflightFinding, preflight_inputs


def _write_miles_xlsx(path: Path, rows: list[dict[str, Any]]) -> None:
    df = pd.DataFrame(rows)
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="ELD Daily Summary", index=False)


def _write_fuel_xlsx(path: Path, rows: list[dict[str, Any]]) -> None:
    df = pd.DataFrame(rows)
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="Fuel Transactions", index=False)


def _codes(findings: list[PreflightFinding]) -> set[str]:
    return {f.code for f in findings}


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_clean_inbox_has_no_errors(tmp_path: Path) -> None:
    _write_miles_xlsx(
        tmp_path / "miles.xlsx",
        [
            {
                "date": "2026-04-01",
                "truck_id": "T1",
                "driver": "John Doe",
                "state": "CA",
                "miles": 500,
            },
            {
                "date": "2026-04-02",
                "truck_id": "T1",
                "driver": "John Doe",
                "state": "NV",
                "miles": 300,
            },
            {
                "date": "2026-04-03",
                "truck_id": "T2",
                "driver": "Jane Doe",
                "state": "AZ",
                "miles": 400,
            },
            {
                "date": "2026-04-04",
                "truck_id": "T2",
                "driver": "Jane Doe",
                "state": "CA",
                "miles": 250,
            },
            {
                "date": "2026-04-05",
                "truck_id": "T1",
                "driver": "John Doe",
                "state": "TX",
                "miles": 600,
            },
            {
                "date": "2026-04-06",
                "truck_id": "T2",
                "driver": "Jane Doe",
                "state": "NM",
                "miles": 350,
            },
        ],
    )
    _write_fuel_xlsx(
        tmp_path / "fuel.xlsx",
        [
            {
                "date": "2026-04-01",
                "card_number": "0001",
                "truck_id": "T1",
                "state": "CA",
                "gallons": 70,
                "tax_paid": 68,
            },
            {
                "date": "2026-04-03",
                "card_number": "0002",
                "truck_id": "T2",
                "state": "AZ",
                "gallons": 65,
                "tax_paid": 17,
            },
            {
                "date": "2026-04-05",
                "card_number": "0001",
                "truck_id": "T1",
                "state": "TX",
                "gallons": 80,
                "tax_paid": 16,
            },
        ],
    )

    report = preflight_inputs(tmp_path)
    assert not report.has_errors
    assert sorted(report.trucks_in_miles) == ["T1", "T2"]
    assert sorted(report.trucks_in_fuel) == ["T1", "T2"]
    assert report.drivers == {"T1": "John Doe", "T2": "Jane Doe"}
    assert report.cards == {"T1": "0001", "T2": "0002"}


# ---------------------------------------------------------------------------
# Sad paths
# ---------------------------------------------------------------------------


def test_missing_inbox_returns_error(tmp_path: Path) -> None:
    report = preflight_inputs(tmp_path / "does_not_exist")
    assert report.has_errors
    assert "INBOX_MISSING" in _codes(report.findings)


def test_empty_inbox_returns_error(tmp_path: Path) -> None:
    report = preflight_inputs(tmp_path)
    assert report.has_errors
    assert "INBOX_EMPTY" in _codes(report.findings)


def test_missing_fuel_file_flagged(tmp_path: Path) -> None:
    _write_miles_xlsx(
        tmp_path / "miles_only.xlsx",
        [
            {"date": "2026-04-01", "truck_id": "T1", "state": "CA", "miles": 500},
            {"date": "2026-04-02", "truck_id": "T1", "state": "NV", "miles": 300},
        ],
    )
    report = preflight_inputs(tmp_path)
    assert report.has_errors
    assert "NO_FUEL" in _codes(report.findings)


def test_truck_id_mismatch_warns(tmp_path: Path) -> None:
    """T2 has fuel but no miles → TRUCKS_ONLY_IN_FUEL warning."""
    _write_miles_xlsx(
        tmp_path / "miles.xlsx",
        [
            {"date": "2026-04-01", "truck_id": "T1", "state": "CA", "miles": 500},
            {"date": "2026-04-02", "truck_id": "T1", "state": "NV", "miles": 300},
        ],
    )
    _write_fuel_xlsx(
        tmp_path / "fuel.xlsx",
        [
            {
                "date": "2026-04-01",
                "card_number": "0001",
                "truck_id": "T1",
                "state": "CA",
                "gallons": 70,
                "tax_paid": 68,
            },
            {
                "date": "2026-04-02",
                "card_number": "0002",
                "truck_id": "T2",
                "state": "AZ",
                "gallons": 65,
                "tax_paid": 17,
            },
            {
                "date": "2026-04-05",
                "card_number": "0002",
                "truck_id": "T2",
                "state": "TX",
                "gallons": 80,
                "tax_paid": 16,
            },
        ],
    )

    report = preflight_inputs(tmp_path)
    assert not report.has_errors  # warning, not error
    assert "TRUCKS_ONLY_IN_FUEL" in _codes(report.findings)


def test_unsupported_file_type_warns(tmp_path: Path) -> None:
    """A .txt file in the inbox is unsupported; warn but still process miles+fuel."""
    (tmp_path / "notes.txt").write_text("just notes\n")
    _write_miles_xlsx(
        tmp_path / "miles.xlsx",
        [{"date": "2026-04-01", "truck_id": "T1", "state": "CA", "miles": 500}],
    )
    _write_fuel_xlsx(
        tmp_path / "fuel.xlsx",
        [
            {
                "date": "2026-04-01",
                "card_number": "1",
                "truck_id": "T1",
                "state": "CA",
                "gallons": 70,
                "tax_paid": 68,
            }
        ],
    )

    report = preflight_inputs(tmp_path)
    assert "UNSUPPORTED_FILE" in _codes(report.findings)
    # Still able to compute since there ARE miles + fuel files
    assert "T1" in report.trucks_in_miles


def test_report_serializes_to_dict(tmp_path: Path) -> None:
    """to_dict() output is JSON-serializable (agent passes this through)."""
    import json

    _write_miles_xlsx(
        tmp_path / "miles.xlsx",
        [{"date": "2026-04-01", "truck_id": "T1", "state": "CA", "miles": 500}],
    )
    _write_fuel_xlsx(
        tmp_path / "fuel.xlsx",
        [
            {
                "date": "2026-04-01",
                "card_number": "1",
                "truck_id": "T1",
                "state": "CA",
                "gallons": 70,
                "tax_paid": 68,
            }
        ],
    )
    report = preflight_inputs(tmp_path)
    payload = report.to_dict()
    # Must serialize cleanly
    json.dumps(payload)
    assert "files" in payload
    assert "findings" in payload
