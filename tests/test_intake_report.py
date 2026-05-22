from __future__ import annotations

import csv
import json
from pathlib import Path

import pandas as pd
from click.testing import CliRunner

from ifta.cli import main
from ifta.intake.report import (
    apply_approved_proposals_csv,
    build_intake_payload,
    extract_existing_fuel_transactions,
    load_receipt_candidates,
    write_intake_outputs,
)


def _write_miles(path: Path) -> None:
    pd.DataFrame(
        [
            {"date": "2026-01-01", "truck": "T1", "state": "AZ", "miles": 250},
            {"date": "2026-03-31", "truck": "T1", "state": "CA", "miles": 250},
        ]
    ).to_excel(path, sheet_name="Mileage", index=False)


def _write_fuel(path: Path) -> None:
    pd.DataFrame(
        [
            {
                "date": "2026-01-19",
                "truck": "T1",
                "state": "AZ",
                "gallons": 80,
                "amount": 360,
                "vendor": "Pilot",
                "card_number": "*2772",
            },
            {
                "date": "2026-03-31",
                "truck": "T1",
                "state": "CA",
                "gallons": 20,
                "amount": 90,
                "vendor": "Love's",
                "card_number": "*2772",
            },
        ]
    ).to_excel(path, sheet_name="Fuel Transactions", index=False)


def _write_receipts(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "receipts": [
                    {
                        "source_file": "jan10_cash.jpg",
                        "date": "2026-01-10",
                        "vendor": "Pilot",
                        "state": "AZ",
                        "gallons": 45.5,
                        "amount": 190.25,
                        "fuel_type": "Diesel",
                        "payment_method": "cash",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_build_intake_payload_proposes_receipt_for_missing_fuel_window(
    tmp_path: Path,
) -> None:
    _write_miles(tmp_path / "miles.xlsx")
    _write_fuel(tmp_path / "fuel.xlsx")
    receipt_path = tmp_path / "receipt_candidates.json"
    _write_receipts(receipt_path)

    payload, proposals = build_intake_payload(
        tmp_path,
        quarter="Q1-2026",
        receipt_candidates_path=receipt_path,
    )

    assert payload["status"] == "NEEDS_APPROVAL"
    assert payload["date_ranges"] == {
        "mileage_start": "2026-01-01",
        "mileage_end": "2026-03-31",
        "fuel_start": "2026-01-19",
        "fuel_end": "2026-03-31",
    }
    assert [gap["kind"] for gap in payload["missing_fuel_gaps"]] == ["fuel_date_gap"]
    assert len(payload["receipt_reviews"]) == 1
    assert len(proposals) == 1
    assert proposals[0].source_file == "jan10_cash.jpg"
    assert proposals[0].truck_id == "T1"
    assert proposals[0].allocation == "suggested_truck"
    assert proposals[0].status == "PROPOSED_NEEDS_APPROVAL"


def test_write_intake_outputs_removes_stale_proposal_csv_when_no_proposals(
    tmp_path: Path,
) -> None:
    stale = tmp_path / "proposed_fuel_additions.csv"
    stale.write_text("old", encoding="utf-8")

    paths = write_intake_outputs(
        {
            "quarter": "Q1-2026",
            "status": "READY_TO_PROCESS",
            "preflight": {"files": [], "findings": []},
            "missing_fuel_gaps": [],
            "receipt_reviews": [],
            "proposed_fuel_additions": [],
        },
        [],
        tmp_path,
    )

    assert paths == {
        "json": tmp_path / "intake_report.json",
        "markdown": tmp_path / "intake_report.md",
    }
    assert not stale.exists()


def test_apply_approved_proposals_csv_writes_only_approved_rows(tmp_path: Path) -> None:
    proposed = tmp_path / "proposed_fuel_additions.csv"
    with proposed.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "approved",
                "source_file",
                "date",
                "truck_id",
                "state",
                "gallons",
                "allocation",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "approved": "yes",
                "source_file": "jan10_cash.jpg",
                "date": "2026-01-10",
                "truck_id": "T1",
                "state": "AZ",
                "gallons": "45.5",
                "allocation": "suggested_truck",
            }
        )
        writer.writerow(
            {
                "approved": "no",
                "source_file": "jan11_cash.jpg",
                "date": "2026-01-11",
                "truck_id": "T1",
                "state": "AZ",
                "gallons": "40",
                "allocation": "suggested_truck",
            }
        )

    count = apply_approved_proposals_csv(
        proposed,
        tmp_path / "derived_fuel_from_receipts.csv",
    )

    assert count == 1
    rows = list(csv.DictReader((tmp_path / "derived_fuel_from_receipts.csv").open()))
    assert rows == [
        {
            "date": "2026-01-10",
            "truck_id": "T1",
            "state": "AZ",
            "gallons": "45.5",
            "tax_paid": "0",
            "source_file": "jan10_cash.jpg",
            "allocation": "suggested_truck",
        }
    ]


def test_extract_existing_fuel_transactions_reads_date_aware_rows(
    tmp_path: Path,
) -> None:
    _write_fuel(tmp_path / "fuel.xlsx")

    transactions = extract_existing_fuel_transactions(tmp_path)

    assert len(transactions) == 2
    assert transactions[0].date == "2026-01-19"
    assert transactions[0].state == "AZ"
    assert transactions[0].gallons == 80.0
    assert transactions[0].truck_id == "T1"
    assert transactions[0].card_last4 == "2772"


def test_load_receipt_candidates_accepts_list_or_receipts_wrapper(tmp_path: Path) -> None:
    wrapper_path = tmp_path / "wrapped.json"
    _write_receipts(wrapper_path)
    list_path = tmp_path / "list.json"
    list_path.write_text(
        json.dumps([{"source_file": "receipt.jpg", "date": "2026-01-10"}]),
        encoding="utf-8",
    )

    wrapped = load_receipt_candidates(wrapper_path)
    as_list = load_receipt_candidates(list_path)

    assert wrapped[0].source_file == "jan10_cash.jpg"
    assert as_list[0].source_file == "receipt.jpg"


def test_intake_cli_writes_report_and_proposal_csv(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    out_dir = tmp_path / "out"
    inbox.mkdir()
    _write_miles(inbox / "miles.xlsx")
    _write_fuel(inbox / "fuel.xlsx")
    receipt_path = inbox / "receipt_candidates.json"
    _write_receipts(receipt_path)

    result = CliRunner().invoke(
        main,
        [
            "intake",
            "--quarter",
            "Q1-2026",
            "--inbox",
            str(inbox),
            "--out",
            str(out_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert (out_dir / "intake_report.json").exists()
    assert (out_dir / "intake_report.md").exists()
    assert (out_dir / "proposed_fuel_additions.csv").exists()
    assert "proposed additions: 1" in result.output


def test_intake_apply_cli_writes_approved_receipt_fuel(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    out_path = inbox / "derived_fuel_from_receipts.csv"
    proposed = tmp_path / "proposed_fuel_additions.csv"
    inbox.mkdir()
    proposed.write_text(
        "\n".join(
            [
                "approved,source_file,date,truck_id,state,gallons,allocation",
                "yes,jan10_cash.jpg,2026-01-10,T1,AZ,45.5,suggested_truck",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        [
            "intake-apply",
            "--quarter",
            "Q1-2026",
            "--inbox",
            str(inbox),
            "--proposed",
            str(proposed),
            "--out",
            str(out_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "approved rows written: 1" in result.output
    assert "2026-01-10,T1,AZ,45.5,0,jan10_cash.jpg,suggested_truck" in out_path.read_text(
        encoding="utf-8"
    )
