"""Reconcile receipt evidence against missing fuel signals.

This module does not OCR receipts and does not silently mutate filing data.
It turns already-reviewed receipt candidates into auditable proposed fuel
additions when they plausibly fill a missing fuel gap.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from ifta.intake.receipts import (
    ExistingFuelTransaction,
    ReceiptReview,
    find_duplicate,
)

GapKind = Literal["fuel_date_gap", "raw_mpg_high"]
AdditionAllocation = Literal["truck", "suggested_truck", "fleet_only"]
AdditionStatus = Literal["PROPOSED_READY", "PROPOSED_NEEDS_APPROVAL"]


@dataclass(frozen=True)
class MissingFuelGap:
    kind: GapKind
    start_date: str | None = None
    end_date: str | None = None
    message: str = ""
    estimated_missing_gallons_min: float | None = None

    def contains(self, date_text: str | None) -> bool:
        if self.start_date is None or self.end_date is None:
            return self.kind == "raw_mpg_high"
        value = _parse_date(date_text)
        start = _parse_date(self.start_date)
        end = _parse_date(self.end_date)
        return value is not None and start is not None and end is not None and start <= value <= end


@dataclass(frozen=True)
class ProposedFuelAddition:
    source_file: str
    date: str
    state: str
    gallons: float
    amount: float | None
    truck_id: str | None
    allocation: AdditionAllocation
    status: AdditionStatus
    payment_method: str
    filled_from: dict[str, str] = field(default_factory=dict)
    reason: str = ""
    issues: list[str] = field(default_factory=list)

    @property
    def requires_approval(self) -> bool:
        return self.status == "PROPOSED_NEEDS_APPROVAL"


def detect_fuel_date_gaps(
    *,
    mileage_start: str | date | None,
    mileage_end: str | date | None,
    fuel_start: str | date | None,
    fuel_end: str | date | None,
) -> list[MissingFuelGap]:
    """Return missing fuel date windows compared to the mileage period."""
    m_start = _coerce_date(mileage_start)
    m_end = _coerce_date(mileage_end)
    f_start = _coerce_date(fuel_start)
    f_end = _coerce_date(fuel_end)
    if m_start is None or m_end is None or f_start is None or f_end is None:
        return []

    gaps: list[MissingFuelGap] = []
    if f_start > m_start:
        end = f_start - timedelta(days=1)
        gaps.append(
            MissingFuelGap(
                kind="fuel_date_gap",
                start_date=m_start.isoformat(),
                end_date=end.isoformat(),
                message=(
                    f"Fuel data starts {f_start.isoformat()}, but mileage starts "
                    f"{m_start.isoformat()}."
                ),
            )
        )
    if f_end < m_end:
        start = f_end + timedelta(days=1)
        gaps.append(
            MissingFuelGap(
                kind="fuel_date_gap",
                start_date=start.isoformat(),
                end_date=m_end.isoformat(),
                message=(
                    f"Fuel data ends {f_end.isoformat()}, but mileage ends "
                    f"{m_end.isoformat()}."
                ),
            )
        )
    return gaps


def detect_raw_mpg_gap(
    *,
    total_miles: float,
    total_gallons: float,
    expected_max_mpg: float,
) -> MissingFuelGap | None:
    """Estimate the minimum missing gallons implied by high raw MPG."""
    if total_miles <= 0 or total_gallons <= 0 or expected_max_mpg <= 0:
        return None
    target_gallons = total_miles / expected_max_mpg
    missing = target_gallons - total_gallons
    if missing <= 0:
        return None
    raw_mpg = total_miles / total_gallons
    return MissingFuelGap(
        kind="raw_mpg_high",
        message=(
            f"Raw MPG is {raw_mpg:.2f}; at {expected_max_mpg:.2f} MPG, at least "
            f"{missing:.2f} gallons appear to be missing."
        ),
        estimated_missing_gallons_min=round(missing, 2),
    )


def propose_fuel_additions(
    receipt_reviews: list[ReceiptReview],
    *,
    gaps: list[MissingFuelGap],
    existing_fuel: list[ExistingFuelTransaction] | None = None,
    truck_states: dict[str, set[str]] | None = None,
) -> list[ProposedFuelAddition]:
    """Return receipt-backed fuel additions that plausibly fill missing fuel."""
    proposals: list[ProposedFuelAddition] = []
    existing = existing_fuel or []
    truck_states = truck_states or {}

    for review in receipt_reviews:
        candidate = review.candidate
        if not review.can_include_fleet_after_approval:
            continue
        if candidate.date is None or candidate.state is None or candidate.gallons is None:
            continue
        if find_duplicate(candidate, existing) is not None:
            continue

        matching_gaps = [gap for gap in gaps if gap.contains(candidate.date)]
        if not matching_gaps:
            continue

        allocation, truck_id, filled_from = _allocation_for_receipt(review, truck_states)
        issues = [issue.code for issue in review.issues]
        status: AdditionStatus = (
            "PROPOSED_READY"
            if review.can_auto_include and allocation == "truck" and not issues
            else "PROPOSED_NEEDS_APPROVAL"
        )
        proposals.append(
            ProposedFuelAddition(
                source_file=candidate.source_file,
                date=candidate.date,
                state=candidate.state.upper(),
                gallons=round(candidate.gallons, 3),
                amount=candidate.amount,
                truck_id=truck_id,
                allocation=allocation,
                status=status,
                payment_method=candidate.payment_method,
                filled_from=filled_from,
                reason="; ".join(gap.message for gap in matching_gaps if gap.message),
                issues=issues,
            )
        )
    return proposals


def write_proposed_fuel_additions_csv(
    proposals: list[ProposedFuelAddition], path: Path
) -> Path:
    """Write proposals for review/approval. This is not an applied fuel file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "approved",
                "source_file",
                "date",
                "truck_id",
                "state",
                "gallons",
                "amount",
                "allocation",
                "status",
                "payment_method",
                "reason",
                "issues",
            ],
        )
        writer.writeheader()
        for proposal in proposals:
            writer.writerow(
                {
                    "approved": "no",
                    "source_file": proposal.source_file,
                    "date": proposal.date,
                    "truck_id": proposal.truck_id or "",
                    "state": proposal.state,
                    "gallons": proposal.gallons,
                    "amount": "" if proposal.amount is None else proposal.amount,
                    "allocation": proposal.allocation,
                    "status": proposal.status,
                    "payment_method": proposal.payment_method,
                    "reason": proposal.reason,
                    "issues": ", ".join(proposal.issues),
                }
            )
    return path


def write_approved_fuel_csv(
    proposals: list[ProposedFuelAddition],
    path: Path,
    *,
    approved_source_files: set[str],
    unassigned_truck_id: str = "unassigned_receipt",
) -> Path:
    """Write approved additions in a shape ingest.py can consume."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "date",
                "truck_id",
                "state",
                "gallons",
                "tax_paid",
                "source_file",
                "allocation",
            ],
        )
        writer.writeheader()
        for proposal in proposals:
            if proposal.source_file not in approved_source_files:
                continue
            writer.writerow(
                {
                    "date": proposal.date,
                    "truck_id": proposal.truck_id or unassigned_truck_id,
                    "state": proposal.state,
                    "gallons": proposal.gallons,
                    "tax_paid": 0,
                    "source_file": proposal.source_file,
                    "allocation": proposal.allocation,
                }
            )
    return path


def _allocation_for_receipt(
    review: ReceiptReview, truck_states: dict[str, set[str]]
) -> tuple[AdditionAllocation, str | None, dict[str, str]]:
    candidate = review.candidate
    if candidate.truck_id:
        return "truck", candidate.truck_id, {"truck_id": "receipt"}

    state = candidate.state.upper() if candidate.state else None
    plausible_trucks = [
        truck_id
        for truck_id, states in truck_states.items()
        if state is not None and state in {s.upper() for s in states}
    ]
    if len(plausible_trucks) == 1:
        return "suggested_truck", plausible_trucks[0], {
            "truck_id": "single_truck_with_miles_in_receipt_state"
        }
    return "fleet_only", None, {"truck_id": "unassigned_receipt"}


def _coerce_date(value: str | date | None) -> date | None:
    if isinstance(value, date):
        return value
    return _parse_date(value)


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
