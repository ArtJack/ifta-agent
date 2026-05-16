"""Extract DM Express Inc's full historical IFTA data from the
IFTA 2025 ACTIVED.xlsx workbook.

Outputs:
  data/david_history.json    — per-quarter structured data
  data/david_profile.json    — operating profile (fleet, routes, vendors)

This is the David equivalent of my_truck_history.json / operating_profile.json.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import cast

import pandas as pd

DAVID_XLSX = Path(
    "/Users/eugenemenshikov/Desktop/AI/AI Engineer Learn/IFTA/David/IFTA 2025 ACTIVED.xlsx"
)
OUT_HISTORY = Path("data/clients/dm_express/history.json")
OUT_PROFILE = Path("data/clients/dm_express/profile.json")

# Map sheet names → quarter labels (header row says e.g. "IFTA Q1 2025")
QUARTER_SHEETS = {
    "Q1": "Q1 2025",
    "Q2": "Q2 2025",
    "Q3": "Q3 2025",
    "Q4": "Q4 2025",
    "Q1 2026 ": "Q1 2026",
    "Q2 2026 ": "Q2 2026",
}

FUEL_SHEETS = {
    "Q2 2025": "Q2 FUEL USED",
    "Q3 2025": "Q3 Fuel USED",
}


def to_float(v):
    if v is None or pd.isna(v):
        return 0.0
    s = str(v).strip().replace(",", "").replace("$", "").replace(" ", "")
    if s in ("", "-", "—"):
        return 0.0
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        f = float(s)
    except ValueError:
        return 0.0
    return -f if neg else f


def extract_quarter_sheet(df: pd.DataFrame, quarter_label: str) -> dict:
    """Parse a quarter sheet like Q1, Q2, ... and return:
    - trucks: list of truck IDs in this quarter
    - per_truck: {truck_id: {state: {miles, gallons}}}
    - per_state_total: {state: {miles, gallons}}
    - fleet totals
    """
    # Find the header row — it has STATE/MILES/GALLONS
    df = df.fillna("")
    header_row = None
    for i in range(min(15, len(df))):
        row = [str(c).upper() for c in df.iloc[i].tolist()]
        if "STATE" in row and "MILES" in row:
            header_row = i
            break
    if header_row is None:
        return {"quarter": quarter_label, "error": "no header row found"}

    headers = df.iloc[header_row].tolist()

    # Walk left-to-right; each truck block = TRUCK-ID-CELL, STATE, MILES, GALLONS, MPG
    blocks = []
    i = 0
    while i < len(headers):
        cell = str(headers[i]).strip()
        # Truck ID is a number-ish year (2013, 2015, 2017, 2019, 2016, 55, 07)
        if (cell and cell.replace(".", "").replace(",", "").isdigit()) or cell in (
            "55",
            "07",
            "2013",
            "2015",
            "2016",
            "2017",
            "2019",
        ):
            truck_id = cell
            # next cells should be STATE, MILES, GALLONS, MPG
            blocks.append(
                {
                    "truck_id": truck_id,
                    "col_state": i + 1,
                    "col_miles": i + 2,
                    "col_gallons": i + 3,
                    "col_mpg": i + 4,
                }
            )
            i += 5
            continue
        i += 1

    # Extract per-truck per-state
    per_truck: dict[str, dict[str, dict]] = {}
    states_seen: set[str] = set()
    for blk in blocks:
        truck_id = blk["truck_id"]
        per_truck[truck_id] = {}
        for r in range(header_row + 1, len(df)):
            row = df.iloc[r]
            try:
                state = str(row.iloc[blk["col_state"]]).strip().upper()
            except IndexError:
                continue
            if state in ("TOTAL", "TOTAL ", ""):
                continue
            if len(state) != 2 or not state.isalpha():
                continue
            miles = to_float(row.iloc[blk["col_miles"]]) if blk["col_miles"] < len(row) else 0
            gallons = to_float(row.iloc[blk["col_gallons"]]) if blk["col_gallons"] < len(row) else 0
            if miles == 0 and gallons == 0:
                continue
            per_truck[truck_id][state] = {"miles": miles, "gallons": gallons}
            states_seen.add(state)

    # Aggregate per-state across trucks
    per_state_total: dict[str, dict] = {}
    for tdata in per_truck.values():
        for state, vals in tdata.items():
            agg = per_state_total.setdefault(state, {"miles": 0.0, "gallons": 0.0})
            agg["miles"] += vals["miles"]
            agg["gallons"] += vals["gallons"]

    fleet_miles = sum(v["miles"] for v in per_state_total.values())
    fleet_gallons = sum(v["gallons"] for v in per_state_total.values())
    fleet_mpg = fleet_miles / fleet_gallons if fleet_gallons else None

    return {
        "quarter": quarter_label,
        "trucks": list(per_truck.keys()),
        "states": sorted(states_seen),
        "fleet_miles": round(fleet_miles, 2),
        "fleet_gallons": round(fleet_gallons, 3),
        "fleet_mpg": round(fleet_mpg, 4) if fleet_mpg else None,
        "per_truck": per_truck,
        "per_state_total": per_state_total,
    }


def extract_fuel_vendor_sheet(df: pd.DataFrame, quarter_label: str) -> dict:
    """Parse a 'FUEL USED' sheet — TRUCK | STATE | <vendor1> | <vendor2> | ..."""
    df = df.fillna("")
    if df.empty:
        return {"quarter": quarter_label, "vendors": [], "per_truck": {}}
    headers = [str(c).strip() for c in df.iloc[0].tolist()]
    # Block detection: TRUCK column followed by STATE then 2-3 vendor cols + TOTAL
    blocks = []
    i = 0
    while i < len(headers):
        h = headers[i].upper()
        if h.startswith("TRUCK"):
            # Find subsequent cols until next TRUCK or end
            j = i + 1
            while j < len(headers) and not headers[j].upper().startswith("TRUCK"):
                j += 1
            blocks.append({"start": i, "end": j})
            i = j
        else:
            i += 1

    all_vendors: set[str] = set()
    per_truck: dict[str, dict[str, dict]] = {}

    for blk in blocks:
        cols = headers[blk["start"] : blk["end"]]
        # first col is TRUCK label, next is STATE, then vendor cols, then TOTAL
        if len(cols) < 4:
            continue
        truck_col = blk["start"]
        state_col = blk["start"] + 1
        # Vendor columns are everything between state_col and TOTAL
        vendor_cols = []
        for k in range(blk["start"] + 2, blk["end"]):
            col_name = headers[k]
            if col_name.upper().startswith("TOTAL"):
                break
            vendor_cols.append((k, col_name))
        all_vendors.update(name for _, name in vendor_cols)

        # Find truck id from first data row
        truck_id = None
        for r in range(1, len(df)):
            v = str(df.iloc[r, truck_col]).strip()
            if v and v not in ("nan", "TOTAL"):
                truck_id = v
                break
        if truck_id is None:
            continue

        per_truck.setdefault(truck_id, {})

        for r in range(1, len(df)):
            state = str(df.iloc[r, state_col]).strip().upper()
            if not state or state in ("TOTAL", "TOTAL "):
                continue
            entry = per_truck[truck_id].setdefault(state, {})
            for col_idx, vendor_name in vendor_cols:
                gal = to_float(df.iloc[r, col_idx])
                if gal:
                    entry[vendor_name] = entry.get(vendor_name, 0) + gal

    # Normalize vendor names (TA GALLONS → TA, PILOT GALLONS → Pilot, etc.)
    def norm(v: str) -> str:
        v = v.upper().replace("GALLONS", "").replace("GALLONS2", "").strip()
        v = "".join(c for c in v if c.isalpha())
        return {
            "TA": "TA",
            "PILOT": "Pilot",
            "EDS": "EDS",
            "RELAY": "Relay",
            "REALY": "Relay",  # David's typo for Relay
        }.get(v, v)

    normalized: dict[str, dict[str, dict[str, float]]] = {}
    vendor_totals: dict[str, float] = {}
    for truck, states_data in per_truck.items():
        normalized[truck] = {}
        for state, vendors in states_data.items():
            normed_state: dict[str, float] = {}
            for vname, gal in vendors.items():
                key = norm(vname)
                normed_state[key] = normed_state.get(key, 0) + gal
                vendor_totals[key] = vendor_totals.get(key, 0) + gal
            if normed_state:
                normalized[truck][state] = normed_state

    return {
        "quarter": quarter_label,
        "vendors": sorted({norm(v) for v in all_vendors if v.strip()}),
        "vendor_totals_gallons": {k: round(v, 1) for k, v in vendor_totals.items()},
        "per_truck": normalized,
    }


def main() -> None:
    xl = pd.ExcelFile(DAVID_XLSX)

    history = {}
    for sheet, label in QUARTER_SHEETS.items():
        if sheet not in xl.sheet_names:
            continue
        df = cast(pd.DataFrame, xl.parse(sheet, header=None, dtype=str))
        q = extract_quarter_sheet(df, label)
        if q.get("fleet_miles", 0) > 0:
            history[label] = q

    # Add fuel vendor data into Q2 and Q3 2025
    for q_label, fuel_sheet in FUEL_SHEETS.items():
        if fuel_sheet not in xl.sheet_names or q_label not in history:
            continue
        df = cast(pd.DataFrame, xl.parse(fuel_sheet, header=None, dtype=str))
        fuel = extract_fuel_vendor_sheet(df, q_label)
        history[q_label]["fuel_vendor_breakdown"] = fuel

    OUT_HISTORY.write_text(json.dumps(history, indent=2))
    print(f"Wrote {len(history)} quarters to {OUT_HISTORY}")

    # ---- Build operating profile ----
    truck_appearances: Counter = Counter()
    state_counter: Counter = Counter()
    state_total_miles: Counter = Counter()
    mpgs = []
    miles_per_q = []
    states_per_q = []
    fuel_vendors_seen: set[str] = set()

    for _q, data in history.items():
        for t in data["trucks"]:
            truck_appearances[t] += 1
        for s in data["states"]:
            state_counter[s] += 1
            state_total_miles[s] += data["per_state_total"][s]["miles"]
        if data["fleet_mpg"]:
            mpgs.append(data["fleet_mpg"])
        if data["fleet_miles"]:
            miles_per_q.append(data["fleet_miles"])
        states_per_q.append(len(data["states"]))
        fv = data.get("fuel_vendor_breakdown", {}).get("vendors", [])
        fuel_vendors_seen.update(fv)

    n = len(history)
    profile = {
        "operator": "DM EXPRESS INC",
        "base_state_unknown": "BASE STATE NOT YET CONFIRMED — David has not told us where he files. Ask him directly before processing the first real return.",
        "fleet_evolution": {
            "trucks_ever_seen": sorted(truck_appearances.keys()),
            "appearances_per_truck": dict(truck_appearances),
            "note": "Truck IDs appear to be year-of-truck (2013, 2015, 2016, 2017, 2019). New units '55' and '07' appeared starting Q1 2026 — fleet expanded.",
        },
        "fuel_vendors": {
            "seen_in_history": sorted(fuel_vendors_seen),
            "note": "Q2 2025 used TA + Pilot + EDS. Q3 2025 dropped TA, added Relay. Sheet header has 'Realy' typo in original.",
        },
        "fleet_mpg_history": {
            "values_by_quarter": {
                q: data["fleet_mpg"] for q, data in history.items() if data["fleet_mpg"]
            },
            "min": round(min(mpgs), 2) if mpgs else None,
            "max": round(max(mpgs), 2) if mpgs else None,
            "mean": round(sum(mpgs) / len(mpgs), 2) if mpgs else None,
        },
        "miles_per_quarter": {
            "values": {
                q: data["fleet_miles"] for q, data in history.items() if data["fleet_miles"]
            },
            "min": round(min(miles_per_q)) if miles_per_q else None,
            "max": round(max(miles_per_q)) if miles_per_q else None,
            "mean": round(sum(miles_per_q) / len(miles_per_q)) if miles_per_q else None,
        },
        "routes": {
            "always_visited": sorted([s for s, c in state_counter.items() if c >= n * 0.8]),
            "frequently_visited": sorted(
                [s for s, c in state_counter.items() if n * 0.4 <= c < n * 0.8]
            ),
            "occasional": sorted([s for s, c in state_counter.items() if 2 <= c < n * 0.4]),
            "one_off": sorted([s for s, c in state_counter.items() if c == 1]),
            "top_5_by_total_miles": sorted(state_total_miles.items(), key=lambda kv: -kv[1])[:5],
        },
        "anomalies_and_patterns": [
            "FLEET CHANGED Q1 2026: added 3 trucks (2016, '55', '07'). MPG may shift as new units enter.",
            "FUEL VENDOR SWITCH Q3 2025: TA replaced with Relay. Verify per-vendor receipts are kept for audit.",
            "Sheet headers in Q1/Q2 2026 say 'IFTA Q4 2025' — copy-paste typo by David in his template. Cosmetic.",
            "California is the largest state by miles in every quarter (largest rate × largest miles). Largest tax contributor.",
            "Heavy reliance on AZ/NV/NM/WY for fuel purchases — these tend to produce CREDIT lines on the return.",
            "Surcharge states KY and VA appear sporadically — make sure surcharge lines are added when miles are present.",
        ],
        "filing_workflow": {
            "raw_inputs_typically_sent": [
                "Excel workbook with per-truck-per-state mileage (David's IFTA 2025 ACTIVED.xlsx template)",
                "Fuel-card transaction PDF (Comdata-style 'IFTA Report' with per-purchase detail)",
                "Per-state aggregated fuel CSV (Truck #, merchant_state, tax_paid, fuel_volume)",
            ],
            "deliver_back": [
                "Review Excel (per-truck blocks + Jurisdiction Summary)",
                "Portal-ready CSV (IFTA standard format)",
                "AI agent review notes (markdown)",
            ],
        },
    }

    OUT_PROFILE.write_text(json.dumps(profile, indent=2))
    print(f"Wrote profile to {OUT_PROFILE}")

    print("\nSummary:")
    for q, data in history.items():
        trucks = data["trucks"]
        print(
            f"  {q}: trucks={trucks}, states={len(data['states'])}, miles={data['fleet_miles']:.0f}, MPG={data['fleet_mpg']}"
        )


if __name__ == "__main__":
    main()
