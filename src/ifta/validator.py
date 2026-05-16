"""Deterministic pre-flight checks on the computed IFTA return.

The agent uses these findings (plus the KB) to write a review note.
We separate hard ERRORs (filing-blocking) from soft WARNINGs (looks-funny).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ifta.calc import IftaReturn
from ifta.models import CleanData

Severity = Literal["error", "warning", "info"]

KB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "regulations.json"


@dataclass
class Finding:
    severity: Severity
    code: str
    message: str
    state: str | None = None
    truck_id: str | None = None


def load_kb() -> dict:
    return json.loads(KB_PATH.read_text())


def validate(data: CleanData, ret: IftaReturn) -> list[Finding]:
    kb = load_kb()
    findings: list[Finding] = []

    if ret.rate_fallback_used:
        findings.append(
            Finding(
                "warning",
                "RATE_FALLBACK",
                ret.rate_warning
                or "Requested-quarter IFTA rates were unavailable; fallback rates were used.",
            )
        )

    # ---- fleet MPG sanity ----
    sanity = kb["fleet_mpg_calculation"]["sanity_range"]
    mpg_lo = sanity["min_realistic_heavy_diesel"]
    mpg_hi = sanity["max_realistic_heavy_diesel"]
    if ret.fleet_mpg == 0:
        findings.append(Finding("error", "MPG_ZERO", "Fleet MPG is 0 — no fuel data parsed."))
    elif ret.fleet_mpg < mpg_lo:
        findings.append(
            Finding(
                "warning",
                "MPG_LOW",
                f"Fleet MPG {ret.fleet_mpg:.2f} is below {mpg_lo} — likely missing miles "
                "or duplicate fuel entries.",
            )
        )
    elif ret.fleet_mpg > mpg_hi:
        findings.append(
            Finding(
                "warning",
                "MPG_HIGH",
                f"Fleet MPG {ret.fleet_mpg:.2f} is above {mpg_hi} — likely missing fuel "
                "purchases or duplicate mileage rows.",
            )
        )

    # ---- negative miles ----
    for mileage_record in data.miles:
        if mileage_record.miles < 0:
            findings.append(
                Finding(
                    "error",
                    "NEG_MILES",
                    f"Negative miles ({mileage_record.miles}) for truck "
                    f"{mileage_record.truck_id} in {mileage_record.state}.",
                    state=mileage_record.state,
                    truck_id=mileage_record.truck_id,
                )
            )

    # ---- fuel without miles (per truck per state) ----
    miles_idx = {
        (mileage_record.truck_id, mileage_record.state)
        for mileage_record in data.miles
        if mileage_record.miles > 0
    }
    for fuel_record in data.fuel:
        if fuel_record.gallons > 0 and (fuel_record.truck_id, fuel_record.state) not in miles_idx:
            findings.append(
                Finding(
                    "warning",
                    "FUEL_NO_MILES",
                    f"Truck {fuel_record.truck_id} bought {fuel_record.gallons:.0f} "
                    f"gal in {fuel_record.state} "
                    "but reported 0 miles there — verify the fuel-card transaction.",
                    state=fuel_record.state,
                    truck_id=fuel_record.truck_id,
                )
            )

    # ---- surcharge states ----
    # Filter out non-state keys (notes, etc.) — real keys are 2-letter codes.
    surcharge_states = {k for k in kb["surcharge_states"] if len(k) == 2}
    states_in_return = {line.state for line in ret.lines}
    surcharge_lines = {line.state for line in ret.lines if line.is_surcharge}
    for ss in surcharge_states & states_in_return:
        if ss not in surcharge_lines:
            findings.append(
                Finding(
                    "warning",
                    "SURCHARGE_MISSING",
                    f"{ss} requires a separate surcharge line on the IFTA return, "
                    "but no surcharge line was computed.",
                    state=ss,
                )
            )
            continue

        findings.append(
            Finding(
                "info",
                "SURCHARGE_INCLUDED",
                f"{ss} surcharge line is included. Verify it matches the state portal.",
                state=ss,
            )
        )

    # ---- Oregon ----
    if "OR" in states_in_return:
        findings.append(
            Finding(
                "info",
                "OREGON_WMT",
                "Oregon uses a weight-mile tax filed directly with ODOT — IFTA tax due is 0. "
                "Miles still report for fleet-MPG.",
                state="OR",
            )
        )

    # ---- non-IFTA states with reported miles (data probably wrong) ----
    non_ifta = set(kb["special_states"]["non_ifta_jurisdictions"]["list"])
    for line in ret.lines:
        if line.state in non_ifta and line.miles > 0:
            findings.append(
                Finding(
                    "warning",
                    "NON_IFTA_MILES",
                    f"{line.state} is non-IFTA but has {line.miles:.0f} miles reported. "
                    "Confirm jurisdiction code.",
                    state=line.state,
                )
            )

    # ---- missing tax rate for a state with miles ----
    for line in ret.lines:
        if line.state not in non_ifta and line.state != "OR" and line.rate == 0 and line.miles > 0:
            findings.append(
                Finding(
                    "warning",
                    "RATE_MISSING",
                    f"No tax rate loaded for {line.state} — check rate matrix.",
                    state=line.state,
                )
            )

    return findings


def format_findings(findings: list[Finding]) -> str:
    if not findings:
        return "No issues found."
    by_sev: dict[str, list[Finding]] = {"error": [], "warning": [], "info": []}
    for f in findings:
        by_sev[f.severity].append(f)
    parts = []
    for sev in ("error", "warning", "info"):
        items = by_sev[sev]
        if not items:
            continue
        parts.append(f"\n{sev.upper()}S ({len(items)}):")
        for f in items:
            tag = f"[{f.code}]"
            loc = f" ({f.state})" if f.state else ""
            parts.append(f"  {tag}{loc} {f.message}")
    return "\n".join(parts).strip()
