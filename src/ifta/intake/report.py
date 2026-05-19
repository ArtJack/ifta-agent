"""Build intake reports before the IFTA filing pipeline runs."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict, fields
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pdfplumber

from ifta.client import quarter_key
from ifta.ingest import (
    _cell_str,
    _classify_column,
    _find_header_row,
    _first_nonempty_cell,
    _read_any,
    _to_float,
    ingest_folder,
)
from ifta.intake.receipts import (
    ExistingFuelTransaction,
    ReceiptCandidate,
    receipt_review_table,
    review_receipt,
)
from ifta.intake.reconcile import (
    MissingFuelGap,
    ProposedFuelAddition,
    detect_fuel_date_gaps,
    detect_raw_mpg_gap,
    propose_fuel_additions,
    write_proposed_fuel_additions_csv,
)
from ifta.models import CleanData, normalize_state
from ifta.preflight import PreflightReport, preflight_inputs
from ifta.validator import load_kb


def build_intake_payload(
    inbox: Path,
    *,
    quarter: str,
    receipt_candidates_path: Path | None = None,
) -> tuple[dict[str, Any], list[ProposedFuelAddition]]:
    """Inspect raw inputs and prepare proposed receipt-backed additions."""
    qkey = quarter_key(quarter)
    report = preflight_inputs(inbox)
    data = ingest_folder(inbox) if inbox.exists() else CleanData()

    receipt_candidates = load_receipt_candidates(receipt_candidates_path)
    existing_fuel = extract_existing_fuel_transactions(inbox) if inbox.exists() else []
    truck_states = _truck_states(data)
    card_truck_map = _card_truck_map(data)
    q_start, q_end = _quarter_date_bounds(qkey)
    receipt_reviews = [
        review_receipt(
            candidate,
            quarter_start=q_start,
            quarter_end=q_end,
            card_truck_map=card_truck_map,
            truck_states=truck_states,
            existing_fuel=existing_fuel,
        )
        for candidate in receipt_candidates
    ]

    gaps = _detect_gaps(inbox, report)
    proposals = propose_fuel_additions(
        receipt_reviews,
        gaps=gaps,
        existing_fuel=existing_fuel,
        truck_states=truck_states,
    )
    payload = {
        "quarter": qkey,
        "inbox": str(inbox),
        "receipt_candidates_path": str(receipt_candidates_path)
        if receipt_candidates_path is not None
        else None,
        "preflight": report.to_dict(),
        "date_ranges": _date_ranges_payload(inbox, report),
        "missing_fuel_gaps": [asdict(gap) for gap in gaps],
        "existing_fuel_transactions_count": len(existing_fuel),
        "receipt_reviews": receipt_review_table(receipt_reviews),
        "proposed_fuel_additions": [_proposal_dict(proposal) for proposal in proposals],
        "status": _intake_status(report, gaps, proposals),
    }
    return payload, proposals


def write_intake_outputs(
    payload: dict[str, Any],
    proposals: list[ProposedFuelAddition],
    out_dir: Path,
) -> dict[str, Path]:
    """Write intake_report.json/md and proposed_fuel_additions.csv."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "intake_report.json"
    md_path = out_dir / "intake_report.md"
    proposed_path = out_dir / "proposed_fuel_additions.csv"

    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(_render_intake_markdown(payload), encoding="utf-8")
    if proposals:
        write_proposed_fuel_additions_csv(proposals, proposed_path)
    elif proposed_path.exists():
        proposed_path.unlink()

    paths = {"json": json_path, "markdown": md_path}
    if proposals:
        paths["proposed_csv"] = proposed_path
    return paths


def load_receipt_candidates(path: Path | None) -> list[ReceiptCandidate]:
    """Load OCR/vision/manual receipt candidates from JSON."""
    if path is None or not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("receipts", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("receipt candidates JSON must be a list or contain a 'receipts' list.")
    allowed = {field.name for field in fields(ReceiptCandidate)}
    return [
        ReceiptCandidate(**{k: v for k, v in row.items() if k in allowed})
        for row in rows
        if isinstance(row, dict)
    ]


def extract_existing_fuel_transactions(inbox: Path) -> list[ExistingFuelTransaction]:
    """Best-effort date-aware fuel transaction extraction for duplicate matching."""
    transactions: list[ExistingFuelTransaction] = []
    for path in sorted(inbox.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() not in {".csv", ".xlsx", ".xlsm", ".xls", ".pdf"}:
            continue
        try:
            sheets = _read_any(path)
        except Exception:
            continue
        for df in sheets.values():
            header_row = _find_header_row(df)
            if header_row is None:
                continue
            headers = df.iloc[header_row].tolist()
            roles = _roles_for_headers(headers)
            if "state" not in roles or "gallons" not in roles or "date" not in roles:
                continue
            for row_idx in range(header_row + 1, len(df)):
                row = df.iloc[row_idx]
                if _first_nonempty_cell(row).upper() == "TOTAL":
                    continue
                state = normalize_state(row.iloc[roles["state"]])
                gallons = _to_float(row.iloc[roles["gallons"]])
                date_text = _cell_str(row, roles["date"])
                if not state or not gallons or not date_text:
                    continue
                transactions.append(
                    ExistingFuelTransaction(
                        date=date_text,
                        state=state,
                        gallons=gallons,
                        amount=_to_float(row.iloc[roles["amount"]]) if "amount" in roles else None,
                        vendor=_cell_str(row, roles["vendor"]) if "vendor" in roles else None,
                        city=_cell_str(row, roles["city"]) if "city" in roles else None,
                        truck_id=_cell_str(row, roles["truck"]) if "truck" in roles else None,
                        card_last4=_last4(_cell_str(row, roles["card"]))
                        if "card" in roles
                        else None,
                        invoice=_cell_str(row, roles["invoice"]) if "invoice" in roles else None,
                        source_file=path.name,
                    )
                )
    return transactions


def apply_approved_proposals_csv(
    proposed_path: Path,
    out_path: Path,
    *,
    unassigned_truck_id: str = "unassigned_receipt",
) -> int:
    """Create derived_fuel_from_receipts.csv from approved proposal rows."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    approved_count = 0
    with (
        proposed_path.open(newline="", encoding="utf-8") as in_fh,
        out_path.open("w", newline="", encoding="utf-8") as out_fh,
    ):
        reader = csv.DictReader(in_fh)
        writer = csv.DictWriter(
            out_fh,
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
        for row in reader:
            if str(row.get("approved", "")).strip().lower() not in {"yes", "y", "true", "1"}:
                continue
            approved_count += 1
            writer.writerow(
                {
                    "date": row.get("date", ""),
                    "truck_id": row.get("truck_id") or unassigned_truck_id,
                    "state": row.get("state", ""),
                    "gallons": row.get("gallons", ""),
                    "tax_paid": "0",
                    "source_file": row.get("source_file", ""),
                    "allocation": row.get("allocation", ""),
                }
            )
    return approved_count


def _detect_gaps(inbox: Path, report: PreflightReport) -> list[MissingFuelGap]:
    date_ranges = _date_ranges_payload(inbox, report)
    gaps = detect_fuel_date_gaps(
        mileage_start=date_ranges.get("mileage_start"),
        mileage_end=date_ranges.get("mileage_end"),
        fuel_start=date_ranges.get("fuel_start"),
        fuel_end=date_ranges.get("fuel_end"),
    )
    expected_max = float(
        load_kb()["fleet_mpg_calculation"]["sanity_range"]["max_realistic_heavy_diesel"]
    )
    raw_gap = detect_raw_mpg_gap(
        total_miles=report.total_miles,
        total_gallons=report.total_gallons,
        expected_max_mpg=expected_max,
    )
    if raw_gap is not None:
        gaps.append(raw_gap)
    return gaps


def _date_ranges_payload(inbox: Path, report: PreflightReport) -> dict[str, str | None]:
    mile_dates: list[date] = []
    fuel_dates: list[date] = []
    for preview in report.files:
        path = inbox / preview.name
        dates = _extract_dates_from_file(path)
        if not dates:
            continue
        if preview.parsed_mile_rows:
            mile_dates.extend(dates)
        if preview.parsed_fuel_rows:
            fuel_dates.extend(dates)
    return {
        "mileage_start": min(mile_dates).isoformat() if mile_dates else None,
        "mileage_end": max(mile_dates).isoformat() if mile_dates else None,
        "fuel_start": min(fuel_dates).isoformat() if fuel_dates else None,
        "fuel_end": max(fuel_dates).isoformat() if fuel_dates else None,
    }


def _extract_dates_from_file(path: Path) -> list[date]:
    if not path.exists():
        return []
    texts: list[str] = []
    try:
        if path.suffix.lower() == ".pdf":
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    texts.append(page.extract_text() or "")
        elif path.suffix.lower() in {".csv", ".xlsx", ".xlsm", ".xls"}:
            for df in _read_any(path).values():
                for value in df.to_numpy().ravel():
                    if value is not None:
                        texts.append(str(value))
    except Exception:
        return []
    dates: list[date] = []
    for text in texts:
        dates.extend(_dates_from_text(text))
    return dates


_MONTH_RE = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)


def _dates_from_text(text: str) -> list[date]:
    out: list[date] = []
    patterns = [
        (r"\b\d{4}-\d{2}-\d{2}\b", "%Y-%m-%d"),
        (rf"\b{_MONTH_RE}\s+\d{{1,2}},\s+\d{{4}}\b", "%b %d, %Y"),
        (r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", None),
    ]
    for pattern, fmt in patterns:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            parsed = _parse_date_match(match, fmt)
            if parsed is not None:
                out.append(parsed)
    return out


def _parse_date_match(value: str, fmt: str | None) -> date | None:
    if fmt is not None:
        for candidate_fmt in {fmt, fmt.replace("%b", "%B")}:
            try:
                return datetime.strptime(value, candidate_fmt).date()
            except ValueError:
                continue
        return None
    for candidate_fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(value, candidate_fmt).date()
        except ValueError:
            continue
    return None


def _roles_for_headers(headers: list[object]) -> dict[str, int]:
    roles: dict[str, int] = {}
    for idx, header in enumerate(headers):
        norm = re.sub(r"[^a-z0-9]+", "", str(header or "").lower())
        role = _classify_column(str(header))
        if "date" in norm and "date" not in roles:
            roles["date"] = idx
        elif role and role not in roles:
            roles[role] = idx
        elif any(key in norm for key in ("amount", "total")) and "amount" not in roles:
            roles["amount"] = idx
        elif (
            any(key in norm for key in ("merchant", "vendor", "station")) and "vendor" not in roles
        ):
            roles["vendor"] = idx
        elif "city" in norm and "city" not in roles:
            roles["city"] = idx
        elif "invoice" in norm and "invoice" not in roles:
            roles["invoice"] = idx
    return roles


def _truck_states(data: CleanData) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for row in data.miles:
        out.setdefault(row.truck_id, set()).add(row.state)
    return out


def _card_truck_map(data: CleanData) -> dict[str, str]:
    out: dict[str, str] = {}
    for truck_id, card in data.truck_cards.items():
        last4 = _last4(card)
        if last4:
            out[last4] = truck_id
    return out


def _last4(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D+", "", value)
    return digits[-4:] if len(digits) >= 4 else None


def _quarter_date_bounds(qkey: str) -> tuple[date, date]:
    quarter = int(qkey[1])
    year = int(qkey[3:])
    bounds = {
        1: (date(year, 1, 1), date(year, 3, 31)),
        2: (date(year, 4, 1), date(year, 6, 30)),
        3: (date(year, 7, 1), date(year, 9, 30)),
        4: (date(year, 10, 1), date(year, 12, 31)),
    }
    return bounds[quarter]


def _proposal_dict(proposal: ProposedFuelAddition) -> dict[str, Any]:
    return asdict(proposal)


def _intake_status(
    report: PreflightReport,
    gaps: list[MissingFuelGap],
    proposals: list[ProposedFuelAddition],
) -> str:
    if report.has_errors and not proposals:
        return "BLOCKED"
    if proposals:
        return "NEEDS_APPROVAL"
    if report.has_errors or gaps:
        return "NEEDS_CUSTOMER_INFO"
    if report.findings:
        return "READY_WITH_WARNINGS"
    return "READY_TO_PROCESS"


def _render_intake_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# IFTA Intake Report",
        "",
        f"Quarter: `{payload['quarter']}`",
        f"Status: `{payload['status']}`",
        "",
        "## Files",
    ]
    for file_info in payload["preflight"]["files"]:
        lines.append(
            f"- `{file_info['name']}`: {file_info['note']} "
            f"(miles rows: {file_info['parsed_mile_rows']}, "
            f"fuel rows: {file_info['parsed_fuel_rows']})"
        )

    findings = payload["preflight"]["findings"]
    if findings:
        lines += ["", "## Findings"]
        for finding in findings:
            lines.append(f"- [{finding['severity']}] `{finding['code']}`: {finding['message']}")

    gaps = payload["missing_fuel_gaps"]
    if gaps:
        lines += ["", "## Missing Fuel Signals"]
        for gap in gaps:
            lines.append(f"- `{gap['kind']}`: {gap['message']}")

    reviews = payload["receipt_reviews"]
    if reviews:
        lines += ["", "## Receipt Reviews"]
        for review in reviews:
            lines.append(
                f"- `{review['source_file']}`: `{review['status']}` "
                f"{review['date'] or ''} {review['state'] or ''} "
                f"{review['gallons'] or ''} gal"
            )

    proposals = payload["proposed_fuel_additions"]
    if proposals:
        lines += ["", "## Proposed Fuel Additions"]
        for proposal in proposals:
            lines.append(
                f"- `{proposal['source_file']}`: {proposal['date']} "
                f"{proposal['state']} {proposal['gallons']} gal "
                f"allocation=`{proposal['allocation']}` status=`{proposal['status']}`"
            )
        lines += [
            "",
            "Review `proposed_fuel_additions.csv`, change `approved` to `yes` "
            "only for verified rows, then run `ifta intake-apply`.",
        ]

    return "\n".join(lines) + "\n"
