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
    # Step 8: duplicates are auto-handled (one source kept, the other added
    # to skipped_files), so they ride as a warning rather than blocking the
    # whole submission. The worker dedupes at ingest using skipped_files.
    assert not report.has_errors
    assert "fuel_summary.xlsx" in report.skipped_files
    # The kept file should be the higher-detail one (more rows). When both
    # files have the same row count, alphabetical order breaks the tie —
    # which still leaves `fuel_summary.xlsx` skipped vs `fuel_detail.xlsx`.
    assert "fuel_detail.xlsx" not in report.skipped_files


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


# ─── Step 8: auto-dedup behavior ─────────────────────────────────────────────


def test_preflight_picks_higher_detail_file_to_keep(tmp_path: Path) -> None:
    """When two files have the same parsed total, keep the one with MORE rows
    (the detail export) and skip the smaller (summary). Same reality as the
    May-23 Rotex submission: a 28-row .xlsx transactions detail + a 13-row
    summary PDF — the detail wins, the summary gets skipped."""
    _write_fuel_xlsx(
        tmp_path / "detail.xlsx",
        # 3 rows summing to 280 gal.
        [
            {"truck": "T1", "state": "CA", "gallons": 100},
            {"truck": "T1", "state": "NV", "gallons": 80},
            {"truck": "T2", "state": "CA", "gallons": 100},
        ],
    )
    _write_fuel_xlsx(
        tmp_path / "summary.xlsx",
        # 1 row summing to 280 gal — the rollup the customer also uploaded.
        [{"truck": "T1", "state": "CA", "gallons": 280}],
    )

    report = preflight_inputs(tmp_path)

    assert "DUPLICATE_FUEL_SOURCE" in _codes(report)
    # Detail (3 rows) wins; summary (1 row) is dropped.
    assert report.skipped_files == ["summary.xlsx"]


def test_ingest_skips_files_marked_by_preflight(tmp_path: Path) -> None:
    """The worker's process_submission passes preflight.skipped_files into
    ingest_folder; the dedup decision becomes a real exclusion at parse
    time, so the customer's filing isn't double-counted."""
    from ifta.ingest import ingest_folder

    _write_fuel_xlsx(
        tmp_path / "primary.xlsx",
        [{"truck": "T1", "state": "CA", "gallons": 100}],
    )
    _write_fuel_xlsx(
        tmp_path / "dup.xlsx",
        [{"truck": "T1", "state": "CA", "gallons": 100}],
    )

    full = ingest_folder(tmp_path)
    skipped = ingest_folder(tmp_path, skip_files={"dup.xlsx"})

    assert sum(r.gallons for r in full.fuel) == 200.0  # double-counted
    assert sum(r.gallons for r in skipped.fuel) == 100.0  # correct after skip
