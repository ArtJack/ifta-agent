from __future__ import annotations

from datetime import date

from ifta.intake.receipts import (
    ExistingFuelTransaction,
    ReceiptCandidate,
    receipt_review_table,
    review_receipt,
)


def test_fleet_card_receipt_with_strong_data_can_auto_include() -> None:
    review = review_receipt(
        ReceiptCandidate(
            source_file="receipt.jpg",
            date="2026-01-12",
            vendor="Loves",
            state="CA",
            gallons=120.5,
            amount=540.25,
            fuel_type="Diesel",
            truck_id="2015",
            card_last4="2760",
            payment_method="fleet_card",
            confidence={"date": 0.96, "gallons": 0.98, "state": 0.95},
        ),
        quarter_start=date(2026, 1, 1),
        quarter_end=date(2026, 3, 31),
        card_truck_map={"2760": "2015"},
        truck_states={"2015": {"CA", "NV"}},
    )

    assert review.status == "USABLE_CONFIRMED"
    assert review.can_auto_include


def test_cash_receipt_is_valid_evidence_but_needs_approval() -> None:
    review = review_receipt(
        ReceiptCandidate(
            source_file="cash_receipt.jpg",
            date="2026-01-10",
            vendor="Pilot",
            state="AZ",
            gallons=80.0,
            amount=360.0,
            fuel_type="Diesel",
            truck_id="2017",
            payment_method="cash",
        ),
        quarter_start=date(2026, 1, 1),
        quarter_end=date(2026, 3, 31),
        truck_states={"2017": {"AZ", "CA"}},
    )

    assert review.status == "USABLE_NEEDS_APPROVAL"
    assert review.requires_human_review
    assert [issue.code for issue in review.issues] == ["NON_FLEET_PAYMENT"]


def test_personal_card_receipt_without_truck_needs_truck_review() -> None:
    review = review_receipt(
        ReceiptCandidate(
            source_file="personal_card.jpg",
            date="2026-01-10",
            vendor="Pilot",
            state="AZ",
            gallons=80.0,
            amount=360.0,
            fuel_type="Diesel",
            payment_method="personal_card",
        ),
        quarter_start=date(2026, 1, 1),
        quarter_end=date(2026, 3, 31),
    )

    assert review.status == "NEEDS_REVIEW_TRUCK_UNKNOWN"
    assert {"NON_FLEET_PAYMENT", "TRUCK_UNKNOWN"} == {issue.code for issue in review.issues}


def test_wrong_fleet_card_truck_mapping_needs_review() -> None:
    review = review_receipt(
        ReceiptCandidate(
            source_file="wrong_truck.jpg",
            date="2026-02-01",
            vendor="Loves",
            state="CA",
            gallons=90.0,
            amount=420.0,
            fuel_type="Diesel",
            truck_id="2015",
            card_last4="2745",
            payment_method="fleet_card",
        ),
        quarter_start=date(2026, 1, 1),
        quarter_end=date(2026, 3, 31),
        card_truck_map={"2745": "2017"},
        truck_states={"2015": {"CA"}, "2017": {"CA"}},
    )

    assert review.status == "NEEDS_REVIEW_TRUCK_MISMATCH"
    assert "TRUCK_CARD_MISMATCH" in {issue.code for issue in review.issues}


def test_receipt_for_state_where_truck_has_no_miles_needs_review() -> None:
    review = review_receipt(
        ReceiptCandidate(
            source_file="route_mismatch.jpg",
            date="2026-02-01",
            vendor="Pilot",
            state="TX",
            gallons=90.0,
            amount=420.0,
            fuel_type="Diesel",
            truck_id="2015",
            payment_method="cash",
        ),
        quarter_start=date(2026, 1, 1),
        quarter_end=date(2026, 3, 31),
        truck_states={"2015": {"CA", "NV"}},
    )

    assert review.status == "NEEDS_REVIEW_TRUCK_MISMATCH"
    assert "TRUCK_STATE_MISMATCH" in {issue.code for issue in review.issues}


def test_missing_location_rejects_receipt() -> None:
    review = review_receipt(
        ReceiptCandidate(
            source_file="missing_location.jpg",
            date="2026-02-01",
            gallons=90.0,
            amount=420.0,
            fuel_type="Diesel",
            truck_id="2015",
            payment_method="fleet_card",
        )
    )

    assert review.status == "REJECTED_MISSING_REQUIRED_DATA"
    assert "MISSING_REQUIRED_RECEIPT_DATA" in {issue.code for issue in review.issues}


def test_wrong_quarter_receipt_is_rejected() -> None:
    review = review_receipt(
        ReceiptCandidate(
            source_file="q2_receipt.jpg",
            date="2026-04-01",
            state="CA",
            gallons=90.0,
            amount=420.0,
            fuel_type="Diesel",
            truck_id="2015",
            payment_method="fleet_card",
        ),
        quarter_start=date(2026, 1, 1),
        quarter_end=date(2026, 3, 31),
    )

    assert review.status == "REJECTED_WRONG_QUARTER"


def test_duplicate_receipt_is_reference_only() -> None:
    review = review_receipt(
        ReceiptCandidate(
            source_file="receipt_copy.jpg",
            date="2026-01-12",
            vendor="Loves",
            state="CA",
            gallons=120.5,
            amount=540.25,
            fuel_type="Diesel",
            truck_id="2015",
            card_last4="2760",
            invoice="123",
            payment_method="fleet_card",
        ),
        existing_fuel=[
            ExistingFuelTransaction(
                date="2026-01-12",
                vendor="Loves",
                state="CA",
                gallons=120.5,
                amount=540.25,
                truck_id="2015",
                card_last4="2760",
                invoice="123",
                source_file="fuel_report.xlsx",
            )
        ],
    )

    assert review.status == "DUPLICATE_REFERENCE"
    assert review.duplicate_of is not None
    assert review.duplicate_of.source_file == "fuel_report.xlsx"


def test_low_confidence_receipt_needs_review() -> None:
    review = review_receipt(
        ReceiptCandidate(
            source_file="blurry.jpg",
            date="2026-01-12",
            vendor="Loves",
            state="CA",
            gallons=120.5,
            amount=540.25,
            fuel_type="Diesel",
            truck_id="2015",
            payment_method="fleet_card",
            confidence={"gallons": 0.61},
        )
    )

    assert review.status == "NEEDS_REVIEW_LOW_CONFIDENCE"


def test_receipt_review_table_is_compact() -> None:
    review = review_receipt(
        ReceiptCandidate(
            source_file="cash_receipt.jpg",
            date="2026-01-10",
            vendor="Pilot",
            state="AZ",
            gallons=80.0,
            amount=360.0,
            fuel_type="Diesel",
            truck_id="2017",
            payment_method="cash",
        )
    )

    rows = receipt_review_table([review])

    assert rows == [
        {
            "source_file": "cash_receipt.jpg",
            "date": "2026-01-10",
            "vendor": "Pilot",
            "state": "AZ",
            "gallons": 80.0,
            "amount": 360.0,
            "truck_id": "2017",
            "payment_method": "cash",
            "status": "USABLE_NEEDS_APPROVAL",
            "issues": ["NON_FLEET_PAYMENT"],
            "duplicate_source": None,
        }
    ]
