from __future__ import annotations

from pathlib import Path

import pandas as pd

from ifta.ingest import _classify_column, parse_sheet
from ifta.preflight import preflight_inputs


def _write_fuel_xlsx(path: Path, rows: list[dict[str, object]]) -> None:
    pd.DataFrame(rows).to_excel(path, sheet_name="Fuel Transactions", index=False)


def _write_miles_xlsx(path: Path, rows: list[dict[str, object]]) -> None:
    pd.DataFrame(rows).to_excel(path, sheet_name="Mileage", index=False)


def _codes(report) -> set[str]:
    return {finding.code for finding in report.findings}


def test_state_province_header_is_state_not_vin_truck() -> None:
    assert _classify_column("State/Province") == "state"


def test_customer_mileage_sheet_with_state_province_parses() -> None:
    df = pd.DataFrame(
        [
            ["Truck", "State/Province", "Total Miles"],
            ["55", "AZ", "1374.87"],
            ["55", "TOTAL", "1374.87"],
            ["2019", "CA", "10984.54"],
        ]
    )

    miles, fuel, _, _ = parse_sheet(df)

    assert fuel == []
    assert [(row.truck_id, row.state, row.miles) for row in miles] == [
        ("55", "AZ", 1374.87),
        ("2019", "CA", 10984.54),
    ]


def test_fuel_detail_total_rows_are_not_counted_as_unknown_truck() -> None:
    df = pd.DataFrame(
        [
            ["Date", "Card Number", "Unit", "State", "Fuel Quantity"],
            ["2026-02-15", "*2772", "2013", "AR", 139.51],
            ["TOTAL", "", "", "AR", 139.51],
        ]
    )

    _, fuel, _, _ = parse_sheet(df)

    assert [(row.truck_id, row.state, row.gallons) for row in fuel] == [
        ("2013", "AR", 139.51)
    ]


def test_preflight_flags_receipt_images_as_reference_only(tmp_path: Path) -> None:
    (tmp_path / "receipt.jpg").write_bytes(b"fake image bytes")
    _write_miles_xlsx(
        tmp_path / "miles.xlsx",
        [{"truck": "T1", "state": "CA", "miles": 500}],
    )
    _write_fuel_xlsx(
        tmp_path / "fuel.xlsx",
        [{"truck": "T1", "state": "CA", "gallons": 70}],
    )

    report = preflight_inputs(tmp_path)

    assert "RECEIPT_IMAGE_UNPARSED" in _codes(report)


def test_preflight_flags_duplicate_fuel_sources(tmp_path: Path) -> None:
    _write_miles_xlsx(
        tmp_path / "miles.xlsx",
        [{"truck": "T1", "state": "CA", "miles": 500}],
    )
    _write_fuel_xlsx(
        tmp_path / "fuel_detail.xlsx",
        [{"truck": "T1", "state": "CA", "gallons": 70}],
    )
    _write_fuel_xlsx(
        tmp_path / "fuel_summary.xlsx",
        [{"truck": "unknown", "state": "CA", "gallons": 70}],
    )

    report = preflight_inputs(tmp_path)

    assert "MULTIPLE_FUEL_SOURCES" in _codes(report)
    assert "DUPLICATE_FUEL_SOURCE" in _codes(report)
    assert report.has_errors


def test_preflight_flags_supported_but_unparsed_reference_file(tmp_path: Path) -> None:
    _write_miles_xlsx(
        tmp_path / "miles.xlsx",
        [{"truck": "T1", "state": "CA", "miles": 500}],
    )
    _write_fuel_xlsx(
        tmp_path / "fuel.xlsx",
        [{"truck": "T1", "state": "CA", "gallons": 70}],
    )
    pd.DataFrame([["not", "ifta"], ["hello", "world"]]).to_excel(
        tmp_path / "reference.xlsx", index=False, header=False
    )

    report = preflight_inputs(tmp_path)

    assert "SUPPORTED_FILE_UNPARSED" in _codes(report)


def test_preflight_flags_extreme_raw_mpg_as_warning_not_blocker(tmp_path: Path) -> None:
    """RAW_MPG_HIGH is a *data-quality* signal — files parsed fine, the ratio
    just suggests missing fuel. The agent's domain knowledge interprets it
    correctly and asks the customer for the missing receipts, so it must
    surface as a warning that flows through the pipeline, never as a hard
    block that drops the submission with a developer-style rejection email.
    Earlier behavior of hard-failing this case is what caused the May-23
    Rotex submission to bounce back instead of getting routed to the AI."""
    _write_miles_xlsx(
        tmp_path / "miles.xlsx",
        [{"truck": "T1", "state": "CA", "miles": 1400}],
    )
    _write_fuel_xlsx(
        tmp_path / "fuel.xlsx",
        [{"truck": "T1", "state": "CA", "gallons": 80}],
    )

    report = preflight_inputs(tmp_path)

    assert "RAW_MPG_HIGH" in _codes(report)
    assert not report.has_errors  # NEW: warning, not blocker
    assert any(
        f.severity == "warning" and f.code == "RAW_MPG_HIGH" for f in report.findings
    )
    assert report.raw_mpg == 17.5
