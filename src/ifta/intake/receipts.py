"""Receipt validation and reconciliation.

Image/PDF receipts are evidence, not filing truth. OCR or vision extraction
should produce a ReceiptCandidate, then this module decides whether the
candidate can be used, needs approval, is duplicate/reference-only, or should
be rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

PaymentMethod = Literal["fleet_card", "personal_card", "cash", "unknown"]
ReceiptStatus = Literal[
    "USABLE_CONFIRMED",
    "USABLE_NEEDS_APPROVAL",
    "USABLE_FLEET_ONLY_NEEDS_ALLOCATION",
    "DUPLICATE_REFERENCE",
    "REJECTED_WRONG_QUARTER",
    "REJECTED_NOT_FUEL",
    "REJECTED_MISSING_REQUIRED_DATA",
    "NEEDS_REVIEW_LOW_CONFIDENCE",
    "NEEDS_REVIEW_TRUCK_MISMATCH",
    "NEEDS_REVIEW_TRUCK_UNKNOWN",
    "NEEDS_REVIEW_LOCATION_OR_TRUCK",
    "NEEDS_REVIEW_LOCATION",
]
IssueSeverity = Literal["error", "warning", "info"]


@dataclass(frozen=True)
class ReceiptIssue:
    code: str
    severity: IssueSeverity
    message: str


@dataclass(frozen=True)
class ReceiptCandidate:
    source_file: str
    date: str | None = None
    vendor: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    gallons: float | None = None
    amount: float | None = None
    fuel_type: str | None = None
    truck_id: str | None = None
    driver: str | None = None
    card_last4: str | None = None
    invoice: str | None = None
    payment_method: PaymentMethod = "unknown"
    confidence: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ExistingFuelTransaction:
    date: str
    state: str
    gallons: float
    amount: float | None = None
    vendor: str | None = None
    city: str | None = None
    truck_id: str | None = None
    card_last4: str | None = None
    invoice: str | None = None
    source_file: str | None = None


@dataclass(frozen=True)
class ReceiptReview:
    candidate: ReceiptCandidate
    status: ReceiptStatus
    issues: list[ReceiptIssue]
    duplicate_of: ExistingFuelTransaction | None = None

    @property
    def can_auto_include(self) -> bool:
        return self.status == "USABLE_CONFIRMED"

    @property
    def requires_human_review(self) -> bool:
        return self.status.startswith("NEEDS_REVIEW") or self.status in (
            "USABLE_NEEDS_APPROVAL",
            "USABLE_FLEET_ONLY_NEEDS_ALLOCATION",
        )

    @property
    def can_include_fleet_after_approval(self) -> bool:
        return self.status in (
            "USABLE_CONFIRMED",
            "USABLE_NEEDS_APPROVAL",
            "USABLE_FLEET_ONLY_NEEDS_ALLOCATION",
        )


def review_receipt(
    candidate: ReceiptCandidate,
    *,
    quarter_start: date | None = None,
    quarter_end: date | None = None,
    card_truck_map: dict[str, str] | None = None,
    truck_states: dict[str, set[str]] | None = None,
    existing_fuel: list[ExistingFuelTransaction] | None = None,
) -> ReceiptReview:
    """Validate and reconcile a receipt candidate.

    card_truck_map maps card last-4 to expected truck id. truck_states maps a
    truck id to states where that truck has mileage for the quarter.
    """
    issues: list[ReceiptIssue] = []

    duplicate = find_duplicate(candidate, existing_fuel or [])
    if duplicate is not None:
        return ReceiptReview(
            candidate=candidate,
            status="DUPLICATE_REFERENCE",
            issues=[
                ReceiptIssue(
                    "DUPLICATE_RECEIPT",
                    "info",
                    "Receipt appears to already exist in structured fuel data.",
                )
            ],
            duplicate_of=duplicate,
        )

    required_missing = _missing_required_fields(candidate)
    if required_missing:
        return ReceiptReview(
            candidate=candidate,
            status="REJECTED_MISSING_REQUIRED_DATA",
            issues=[
                ReceiptIssue(
                    "MISSING_REQUIRED_RECEIPT_DATA",
                    "error",
                    f"Missing required receipt fields: {', '.join(required_missing)}.",
                )
            ],
        )

    receipt_date = _parse_date(candidate.date)
    if receipt_date is None:
        return ReceiptReview(
            candidate=candidate,
            status="REJECTED_MISSING_REQUIRED_DATA",
            issues=[
                ReceiptIssue(
                    "INVALID_RECEIPT_DATE",
                    "error",
                    f"Receipt date could not be parsed: {candidate.date}.",
                )
            ],
        )
    if (
        quarter_start is not None
        and quarter_end is not None
        and (receipt_date < quarter_start or receipt_date > quarter_end)
    ):
        return ReceiptReview(
            candidate=candidate,
            status="REJECTED_WRONG_QUARTER",
            issues=[
                ReceiptIssue(
                    "RECEIPT_OUTSIDE_QUARTER",
                    "error",
                    f"Receipt date {receipt_date.isoformat()} is outside the target quarter.",
                )
            ],
        )

    if candidate.fuel_type and "diesel" not in candidate.fuel_type.lower():
        return ReceiptReview(
            candidate=candidate,
            status="REJECTED_NOT_FUEL",
            issues=[
                ReceiptIssue(
                    "NON_DIESEL_RECEIPT",
                    "error",
                    f"Receipt fuel type is {candidate.fuel_type!r}, not diesel.",
                )
            ],
        )

    if candidate.gallons is not None and candidate.gallons <= 0:
        return ReceiptReview(
            candidate=candidate,
            status="REJECTED_MISSING_REQUIRED_DATA",
            issues=[
                ReceiptIssue(
                    "INVALID_GALLONS",
                    "error",
                    f"Receipt gallons must be positive; got {candidate.gallons}.",
                )
            ],
        )

    if _has_low_confidence(candidate):
        issues.append(
            ReceiptIssue(
                "LOW_EXTRACTION_CONFIDENCE",
                "warning",
                "One or more required fields were extracted with low confidence.",
            )
        )

    issues.extend(_price_sanity_issues(candidate))
    issues.extend(_payment_method_issues(candidate))
    truck_issue = _truck_assignment_issue(candidate, card_truck_map or {}, truck_states or {})
    if truck_issue is not None:
        issues.append(truck_issue)

    status = _status_from_issues(candidate, issues)
    return ReceiptReview(candidate=candidate, status=status, issues=issues)


def find_duplicate(
    candidate: ReceiptCandidate,
    existing_fuel: list[ExistingFuelTransaction],
    *,
    date_tolerance_days: int = 1,
    gallon_tolerance: float = 0.05,
    amount_tolerance: float = 0.50,
) -> ExistingFuelTransaction | None:
    """Return the best matching existing transaction, if this is a duplicate."""
    c_date = _parse_date(candidate.date)
    if c_date is None or candidate.gallons is None:
        return None

    for tx in existing_fuel:
        tx_date = _parse_date(tx.date)
        if tx_date is None:
            continue
        if abs((c_date - tx_date).days) > date_tolerance_days:
            continue
        if abs(candidate.gallons - tx.gallons) > gallon_tolerance:
            continue
        if (
            candidate.amount is not None
            and tx.amount is not None
            and abs(candidate.amount - tx.amount) > amount_tolerance
        ):
            continue
        if candidate.invoice and tx.invoice and candidate.invoice == tx.invoice:
            return tx
        if candidate.card_last4 and tx.card_last4 and candidate.card_last4 != tx.card_last4:
            continue
        if candidate.state and tx.state and candidate.state.upper() != tx.state.upper():
            continue
        # Vendor is the primary soft signal: when both records name a vendor,
        # they must agree. City alone is too weak — different fuel stops in
        # the same city on the same day can otherwise produce false matches —
        # so we only fall back to a city match when at least one side has no
        # vendor on record.
        if candidate.vendor and tx.vendor:
            if _same_text(candidate.vendor, tx.vendor):
                return tx
            continue
        if _same_text(candidate.city, tx.city):
            return tx
    return None


def _missing_required_fields(candidate: ReceiptCandidate) -> list[str]:
    missing: list[str] = []
    if not candidate.date:
        missing.append("date")
    if candidate.gallons is None:
        missing.append("gallons")
    if not candidate.state and not candidate.address and not candidate.city:
        missing.append("state_or_location")
    return missing


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _has_low_confidence(candidate: ReceiptCandidate, threshold: float = 0.80) -> bool:
    for field_name in ("date", "gallons", "state", "amount", "truck_id"):
        confidence = candidate.confidence.get(field_name)
        if confidence is not None and confidence < threshold:
            return True
    return False


def _price_sanity_issues(candidate: ReceiptCandidate) -> list[ReceiptIssue]:
    if candidate.amount is None or candidate.gallons is None or candidate.gallons <= 0:
        return []
    ppg = candidate.amount / candidate.gallons
    if ppg < 2.0 or ppg > 8.0:
        return [
            ReceiptIssue(
                "PRICE_PER_GALLON_OUTLIER",
                "warning",
                f"Receipt price per gallon is ${ppg:.2f}; verify OCR and receipt totals.",
            )
        ]
    return []


def _payment_method_issues(candidate: ReceiptCandidate) -> list[ReceiptIssue]:
    if candidate.payment_method in ("cash", "personal_card"):
        return [
            ReceiptIssue(
                "NON_FLEET_PAYMENT",
                "warning",
                "Fuel was paid by cash/personal card. It may be valid fleet fuel, but "
                "needs approval before inclusion.",
            )
        ]
    if candidate.payment_method == "unknown":
        return [
            ReceiptIssue(
                "UNKNOWN_PAYMENT_METHOD",
                "warning",
                "Receipt payment method is unknown. Confirm whether this was fleet card, "
                "cash, or personal card.",
            )
        ]
    return []


def _truck_assignment_issue(
    candidate: ReceiptCandidate,
    card_truck_map: dict[str, str],
    truck_states: dict[str, set[str]],
) -> ReceiptIssue | None:
    if not candidate.truck_id:
        state = candidate.state.upper() if candidate.state else None
        fleet_states = {s.upper() for states in truck_states.values() for s in states}
        if state and fleet_states and state not in fleet_states:
            return ReceiptIssue(
                "NO_FLEET_MILES_IN_RECEIPT_STATE",
                "warning",
                f"Receipt is in {candidate.state}, but no truck has mileage in that state.",
            )
        return ReceiptIssue(
            "FLEET_ONLY_TRUCK_UNASSIGNED",
            "warning",
            "Receipt does not identify a truck/unit. It can support fleet fuel only "
            "after approval; assign manually before using it for per-truck reports.",
        )

    if candidate.card_last4:
        mapped_truck = card_truck_map.get(candidate.card_last4)
        if mapped_truck and mapped_truck != candidate.truck_id:
            return ReceiptIssue(
                "TRUCK_CARD_MISMATCH",
                "error",
                f"Receipt card {candidate.card_last4} is mapped to truck {mapped_truck}, "
                f"but receipt says truck {candidate.truck_id}.",
            )

    states = truck_states.get(candidate.truck_id)
    if states and candidate.state and candidate.state.upper() not in {s.upper() for s in states}:
        return ReceiptIssue(
            "TRUCK_STATE_MISMATCH",
            "warning",
            f"Truck {candidate.truck_id} has no mileage in {candidate.state} for this quarter.",
        )
    return None


def _status_from_issues(candidate: ReceiptCandidate, issues: list[ReceiptIssue]) -> ReceiptStatus:
    codes = {issue.code for issue in issues}
    if "TRUCK_CARD_MISMATCH" in codes or "TRUCK_STATE_MISMATCH" in codes:
        return "NEEDS_REVIEW_TRUCK_MISMATCH"
    if "NO_FLEET_MILES_IN_RECEIPT_STATE" in codes:
        return "NEEDS_REVIEW_LOCATION_OR_TRUCK"
    if "FLEET_ONLY_TRUCK_UNASSIGNED" in codes:
        return "USABLE_FLEET_ONLY_NEEDS_ALLOCATION"
    if "TRUCK_UNKNOWN" in codes:
        return "NEEDS_REVIEW_TRUCK_UNKNOWN"
    if "LOW_EXTRACTION_CONFIDENCE" in codes:
        return "NEEDS_REVIEW_LOW_CONFIDENCE"
    if not candidate.state and (candidate.address or candidate.city):
        return "NEEDS_REVIEW_LOCATION"
    if "NON_FLEET_PAYMENT" in codes or "UNKNOWN_PAYMENT_METHOD" in codes:
        return "USABLE_NEEDS_APPROVAL"
    if any(issue.severity == "warning" for issue in issues):
        return "USABLE_NEEDS_APPROVAL"
    return "USABLE_CONFIRMED"


def _same_text(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return left.strip().lower() == right.strip().lower()


def receipt_review_table(reviews: list[ReceiptReview]) -> list[dict[str, object]]:
    """Compact rows for an intake report or agent review packet."""
    rows: list[dict[str, object]] = []
    for review in reviews:
        candidate = review.candidate
        rows.append(
            {
                "source_file": candidate.source_file,
                "date": candidate.date,
                "vendor": candidate.vendor,
                "state": candidate.state,
                "gallons": candidate.gallons,
                "amount": candidate.amount,
                "truck_id": candidate.truck_id,
                "payment_method": candidate.payment_method,
                "status": review.status,
                "fleet_only": review.status == "USABLE_FLEET_ONLY_NEEDS_ALLOCATION",
                "issues": [issue.code for issue in review.issues],
                "duplicate_source": review.duplicate_of.source_file
                if review.duplicate_of is not None
                else None,
            }
        )
    return rows
