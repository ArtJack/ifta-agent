"""Deterministic review packet for the AI filing-review agent.

The agent should review facts already computed by the pipeline. This module
builds that fact packet and the deterministic filing-status gate that the
model is not allowed to weaken.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Literal

from ifta.calc import IftaReturn, StateLine
from ifta.client import ClientContext
from ifta.models import CleanData
from ifta.validator import Finding, load_kb

FilingStatus = Literal["DO_NOT_FILE", "READY_WITH_WARNINGS", "READY_TO_FILE"]


def determine_filing_status(ret: IftaReturn, findings: list[Finding]) -> dict[str, Any]:
    """Return the deterministic filing gate for a computed return."""
    blocking_reasons: list[str] = []
    warning_reasons: list[str] = []

    if ret.rate_fallback_used:
        blocking_reasons.append(
            ret.rate_warning
            or "Current-quarter IFTA rates were unavailable and fallback rates were used."
        )

    for finding in findings:
        reason = f"[{finding.code}] {finding.message}"
        if finding.severity == "error":
            blocking_reasons.append(reason)
        elif finding.severity == "warning":
            warning_reasons.append(reason)

    if blocking_reasons:
        return {
            "status": "DO_NOT_FILE",
            "reasons": blocking_reasons,
            "warning_reasons": warning_reasons,
        }
    if warning_reasons:
        return {
            "status": "READY_WITH_WARNINGS",
            "reasons": warning_reasons,
            "warning_reasons": warning_reasons,
        }
    return {"status": "READY_TO_FILE", "reasons": [], "warning_reasons": []}


def build_review_packet(
    data: CleanData,
    ret: IftaReturn,
    findings: list[Finding],
    client_context: ClientContext,
) -> dict[str, Any]:
    """Build the deterministic packet the agent must cite in reviews."""
    return {
        "quarter": ret.quarter,
        "client_context": client_context.to_prompt_dict(),
        "filing_status": determine_filing_status(ret, findings),
        "return_summary": _return_summary(ret),
        "rate_status": {
            "requested_quarter": ret.quarter,
            "source_quarter": ret.rate_source_quarter,
            "fallback_used": ret.rate_fallback_used,
            "warning": ret.rate_warning,
        },
        "validator_findings": [_finding_dict(f) for f in findings],
        "top_liabilities": [_line_dict(ln) for ln in _top_lines(ret.lines, positive=True)],
        "top_credits": [_line_dict(ln) for ln in _top_lines(ret.lines, positive=False)],
        "fuel_without_miles": _fuel_without_miles(data),
        "miles_without_fuel": _miles_without_fuel(data),
        "truck_mpg_outliers": _truck_mpg_outliers(ret),
        "surcharge_lines": [_line_dict(ln) for ln in ret.lines if ln.is_surcharge],
        "special_jurisdiction_reminders": _special_jurisdiction_reminders(ret, findings),
        "review_output_schema": {
            "filing_status": "One of DO_NOT_FILE, READY_WITH_WARNINGS, READY_TO_FILE. Must match this packet.",
            "summary": "One paragraph, <=4 sentences, grounded in this packet.",
            "issues": [
                {
                    "severity": "error|warning|info",
                    "code": "stable_issue_code",
                    "claim": "What is wrong or risky.",
                    "evidence": {"source": "review_packet path", "value": "supporting data"},
                    "recommended_action": "Concrete action before filing.",
                    "filing_impact": "Why it affects readiness.",
                }
            ],
            "filing_reminders": [
                {
                    "severity": "info",
                    "code": "stable_reminder_code",
                    "claim": "Reminder.",
                    "evidence": {"source": "review_packet path", "value": "supporting data"},
                    "recommended_action": "Concrete action.",
                    "filing_impact": "Operational impact.",
                }
            ],
            "next_steps": [
                {
                    "severity": "info|warning|error",
                    "code": "stable_todo_code",
                    "claim": "Task.",
                    "evidence": {"source": "review_packet path", "value": "supporting data"},
                    "recommended_action": "What to do next.",
                    "filing_impact": "Result of completing the task.",
                }
            ],
        },
    }


def _return_summary(ret: IftaReturn) -> dict[str, Any]:
    return {
        "fuel": ret.fuel,
        "fleet_miles": round(ret.fleet_miles, 2),
        "fleet_gallons": round(ret.fleet_gallons, 3),
        "fleet_mpg": round(ret.fleet_mpg, 4),
        "total_tax_due": round(ret.total_tax_due, 2),
        "truck_count": len(ret.trucks),
        "jurisdiction_line_count": len(ret.lines),
        "trucks": [
            {
                "truck_id": t.truck_id,
                "miles": round(t.miles, 2),
                "gallons": round(t.gallons, 3),
                "mpg": round(t.mpg, 4),
            }
            for t in ret.trucks
        ],
    }


def _finding_dict(finding: Finding) -> dict[str, Any]:
    return {
        "severity": finding.severity,
        "code": finding.code,
        "message": finding.message,
        "state": finding.state,
        "truck_id": finding.truck_id,
    }


def _line_dict(line: StateLine) -> dict[str, Any]:
    return {
        "state": line.state,
        "label": line.label,
        "is_surcharge": line.is_surcharge,
        "miles": int(round(line.miles)),
        "tax_paid_gal": int(round(line.tax_paid_gal)),
        "taxable_gal": int(round(line.taxable_gal)),
        "net_taxable_gal": int(round(line.net_taxable_gal)),
        "rate": round(line.rate, 4),
        "tax_due": round(line.tax_due, 2),
    }


def _top_lines(lines: list[StateLine], *, positive: bool, limit: int = 5) -> list[StateLine]:
    if positive:
        selected = [ln for ln in lines if ln.tax_due > 0]
        return sorted(selected, key=lambda ln: ln.tax_due, reverse=True)[:limit]
    selected = [ln for ln in lines if ln.tax_due < 0]
    return sorted(selected, key=lambda ln: ln.tax_due)[:limit]


def _fuel_without_miles(data: CleanData) -> list[dict[str, Any]]:
    miles_idx = {
        (record.truck_id, record.state)
        for record in data.miles
        if record.miles > 0
    }
    rows = [
        {
            "truck_id": record.truck_id,
            "state": record.state,
            "gallons": round(record.gallons, 3),
        }
        for record in data.fuel
        if record.gallons > 0 and (record.truck_id, record.state) not in miles_idx
    ]
    return sorted(rows, key=lambda row: (str(row["truck_id"]), str(row["state"])))


def _miles_without_fuel(data: CleanData) -> list[dict[str, Any]]:
    fuel_idx = {
        (record.truck_id, record.state)
        for record in data.fuel
        if record.gallons > 0
    }
    rows = [
        {
            "truck_id": record.truck_id,
            "state": record.state,
            "miles": round(record.miles, 2),
        }
        for record in data.miles
        if record.miles > 0 and (record.truck_id, record.state) not in fuel_idx
    ]
    return sorted(rows, key=lambda row: (str(row["truck_id"]), str(row["state"])))


def _truck_mpg_outliers(ret: IftaReturn) -> list[dict[str, Any]]:
    sanity = load_kb()["fleet_mpg_calculation"]["sanity_range"]
    mpg_lo = float(sanity["min_realistic_heavy_diesel"])
    mpg_hi = float(sanity["max_realistic_heavy_diesel"])
    outliers: list[dict[str, Any]] = []
    for truck in ret.trucks:
        if truck.gallons <= 0 and truck.miles > 0:
            outliers.append(
                {
                    "truck_id": truck.truck_id,
                    "mpg": None,
                    "reason": "Truck has miles but no fuel gallons.",
                    "miles": round(truck.miles, 2),
                    "gallons": round(truck.gallons, 3),
                }
            )
            continue
        mpg = truck.mpg
        if mpg and (mpg < mpg_lo or mpg > mpg_hi):
            outliers.append(
                {
                    "truck_id": truck.truck_id,
                    "mpg": round(mpg, 4),
                    "reason": f"Truck MPG outside generic heavy-diesel range {mpg_lo}-{mpg_hi}.",
                    "miles": round(truck.miles, 2),
                    "gallons": round(truck.gallons, 3),
                }
            )
    return outliers


def _special_jurisdiction_reminders(
    ret: IftaReturn, findings: list[Finding]
) -> list[dict[str, Any]]:
    by_code: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        by_code[finding.code].append(finding)

    reminders: list[dict[str, Any]] = []
    for code in ("OREGON_WMT", "SURCHARGE_INCLUDED", "SURCHARGE_MISSING", "NON_IFTA_MILES"):
        for finding in by_code.get(code, []):
            reminders.append(_finding_dict(finding))

    states = {line.state for line in ret.lines if line.miles > 0}
    if "NY" in states:
        reminders.append(
            {
                "severity": "info",
                "code": "NY_HUT",
                "message": "New York HUT may apply outside IFTA.",
                "state": "NY",
                "truck_id": None,
            }
        )
    if "NM" in states:
        reminders.append(
            {
                "severity": "info",
                "code": "NM_WDT",
                "message": "New Mexico weight distance tax may apply outside IFTA.",
                "state": "NM",
                "truck_id": None,
            }
        )
    return reminders
