from __future__ import annotations

from datetime import date

from ifta.intake.receipts import (
    ExistingFuelTransaction,
    ReceiptCandidate,
    review_receipt,
)
from ifta.intake.reconcile import (
    detect_fuel_date_gaps,
    detect_raw_mpg_gap,
    propose_fuel_additions,
    write_approved_fuel_csv,
    write_proposed_fuel_additions_csv,
)


def test_detect_fuel_date_gap_when_fuel_starts_after_mileage() -> None:
    gaps = detect_fuel_date_gaps(
        mileage_start="2026-01-01",
        mileage_end="2026-03-31",
        fuel_start="2026-01-19",
        fuel_end="2026-03-31",
    )

    assert len(gaps) == 1
    assert gaps[0].kind == "fuel_date_gap"
    assert gaps[0].start_date == "2026-01-01"
    assert gaps[0].end_date == "2026-01-18"
    assert gaps[0].contains("2026-01-10")
    assert not gaps[0].contains("2026-01-19")


def test_detect_raw_mpg_gap_estimates_missing_gallons() -> None:
    gap = detect_raw_mpg_gap(
        total_miles=124_925.99,
        total_gallons=8_753.74,
        expected_max_mpg=10.5,
    )

    assert gap is not None
    assert gap.kind == "raw_mpg_high"
    assert gap.estimated_missing_gallons_min == 3143.97


def test_receipt_in_missing_window_becomes_proposed_addition() -> None:
    review = review_receipt(
        ReceiptCandidate(
            source_file="jan10_cash.jpg",
            date="2026-01-10",
            vendor="Pilot",
            state="AZ",
            gallons=80.0,
            amount=360.0,
            fuel_type="Diesel",
            payment_method="cash",
        ),
        quarter_start=date(2026, 1, 1),
        quarter_end=date(2026, 3, 31),
        truck_states={"2015": {"AZ"}, "2017": {"CA"}},
    )
    gaps = detect_fuel_date_gaps(
        mileage_start="2026-01-01",
        mileage_end="2026-03-31",
        fuel_start="2026-01-19",
        fuel_end="2026-03-31",
    )

    proposals = propose_fuel_additions(
        [review],
        gaps=gaps,
        truck_states={"2015": {"AZ"}, "2017": {"CA"}},
    )

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.source_file == "jan10_cash.jpg"
    assert proposal.truck_id == "2015"
    assert proposal.allocation == "suggested_truck"
    assert proposal.status == "PROPOSED_NEEDS_APPROVAL"
    assert proposal.filled_from == {"truck_id": "single_truck_with_miles_in_receipt_state"}
    assert "NON_FLEET_PAYMENT" in proposal.issues


def test_no_truck_receipt_with_multiple_plausible_trucks_stays_fleet_only() -> None:
    review = review_receipt(
        ReceiptCandidate(
            source_file="jan10_cash.jpg",
            date="2026-01-10",
            vendor="Pilot",
            state="AZ",
            gallons=80.0,
            amount=360.0,
            fuel_type="Diesel",
            payment_method="cash",
        ),
        truck_states={"2015": {"AZ"}, "2017": {"AZ"}},
    )

    proposals = propose_fuel_additions(
        [review],
        gaps=detect_fuel_date_gaps(
            mileage_start="2026-01-01",
            mileage_end="2026-03-31",
            fuel_start="2026-01-19",
            fuel_end="2026-03-31",
        ),
        truck_states={"2015": {"AZ"}, "2017": {"AZ"}},
    )

    assert proposals[0].allocation == "fleet_only"
    assert proposals[0].truck_id is None


def test_duplicate_receipt_does_not_become_proposed_addition() -> None:
    existing = [
        ExistingFuelTransaction(
            date="2026-01-10",
            state="AZ",
            gallons=80.0,
            amount=360.0,
            vendor="Pilot",
            source_file="fuel_report.xlsx",
        )
    ]
    review = review_receipt(
        ReceiptCandidate(
            source_file="jan10_copy.jpg",
            date="2026-01-10",
            vendor="Pilot",
            state="AZ",
            gallons=80.0,
            amount=360.0,
            fuel_type="Diesel",
            payment_method="cash",
        ),
        existing_fuel=existing,
    )

    proposals = propose_fuel_additions(
        [review],
        gaps=detect_fuel_date_gaps(
            mileage_start="2026-01-01",
            mileage_end="2026-03-31",
            fuel_start="2026-01-19",
            fuel_end="2026-03-31",
        ),
        existing_fuel=existing,
    )

    assert proposals == []


def test_receipt_outside_missing_window_is_not_proposed() -> None:
    review = review_receipt(
        ReceiptCandidate(
            source_file="feb10_cash.jpg",
            date="2026-02-10",
            vendor="Pilot",
            state="AZ",
            gallons=80.0,
            amount=360.0,
            fuel_type="Diesel",
            payment_method="cash",
        )
    )

    proposals = propose_fuel_additions(
        [review],
        gaps=detect_fuel_date_gaps(
            mileage_start="2026-01-01",
            mileage_end="2026-03-31",
            fuel_start="2026-01-19",
            fuel_end="2026-03-31",
        ),
    )

    assert proposals == []


def test_write_proposal_and_approved_csvs(tmp_path) -> None:
    review = review_receipt(
        ReceiptCandidate(
            source_file="jan10_cash.jpg",
            date="2026-01-10",
            vendor="Pilot",
            state="AZ",
            gallons=80.0,
            amount=360.0,
            fuel_type="Diesel",
            payment_method="cash",
        ),
        truck_states={"2015": {"AZ"}},
    )
    proposals = propose_fuel_additions(
        [review],
        gaps=detect_fuel_date_gaps(
            mileage_start="2026-01-01",
            mileage_end="2026-03-31",
            fuel_start="2026-01-19",
            fuel_end="2026-03-31",
        ),
        truck_states={"2015": {"AZ"}},
    )

    proposed_path = write_proposed_fuel_additions_csv(
        proposals, tmp_path / "proposed_fuel_additions.csv"
    )
    approved_path = write_approved_fuel_csv(
        proposals,
        tmp_path / "derived_fuel_from_receipts.csv",
        approved_source_files={"jan10_cash.jpg"},
    )

    proposed_text = proposed_path.read_text()
    approved_text = approved_path.read_text()
    assert "approved,source_file,date" in proposed_text
    assert "no,jan10_cash.jpg,2026-01-10" in proposed_text
    assert "date,truck_id,state,gallons" in approved_text
    assert "2026-01-10,2015,AZ,80.0" in approved_text
