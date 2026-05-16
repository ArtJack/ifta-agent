"""Generate realistic raw IFTA test data for Q2 2026.

Fictional carrier: TEST LOGISTICS LLC, 5 trucks (T1–T5), 5 fuel cards
(0001–0005, one per truck). Q2 2026 = Apr/May/Jun 2026.

Activity profile (per month):
    T1: drives 4 weeks (full-time)            ~14–17K mi/mo
    T2: drives 3 weeks                         ~10–13K mi/mo
    T3: drives 3 weeks                         ~10–13K mi/mo
    T4: drives 2 weeks                         ~7–8K  mi/mo
    T5: drives <1 week (intermittent)          ~2–3K  mi/mo

HOS constraints respected:
    - max 700 miles per day (~10 hr shift)
    - max 70 hours per week (DOT 8-day rolling rule)
    - 6 driving days per week max

Outputs (Excel):
    inbox/Q2-2026/test_logistics_miles.xlsx   (ELD daily-state summary)
    inbox/Q2-2026/test_logistics_fuel.xlsx    (fuel-card transactions)

Run:
    .venv/bin/python scripts/generate_test_data.py
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

SEED = 20260601
random.seed(SEED)

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "inbox" / "Q2-2026"
OUT_DIR.mkdir(parents=True, exist_ok=True)

CARRIER = "TEST LOGISTICS LLC"

# ---------------------------------------------------------------------------
# Truck activity profiles
# ---------------------------------------------------------------------------

TRUCKS = [
    # (truck_id, card_number, driver_name, driving_days_per_week, weeks_per_month, base_state)
    ("T1", "0001", "John Smith", 6, 4, "TX"),  # heavy long-haul
    ("T2", "0002", "Mike Johnson", 6, 3, "IL"),  # regional Midwest
    ("T3", "0003", "Carlos Garcia", 6, 3, "CA"),  # west coast regional
    ("T4", "0004", "David Brown", 5, 2, "GA"),  # southeast regional, fewer days
    ("T5", "0005", "Sam Wilson", 4, 1, "OH"),  # local-only, occasional
]

# Realistic operating territories — wider for higher-mileage trucks.
# Mix in KY and VA so surcharge logic exercises. Exclude AK, HI, foreign.
TERRITORIES: dict[str, list[str]] = {
    "T1": [  # coast-to-coast long-haul: ~28 states
        "TX",
        "OK",
        "AR",
        "TN",
        "GA",
        "FL",
        "AL",
        "MS",
        "LA",
        "NM",
        "AZ",
        "CA",
        "NV",
        "UT",
        "CO",
        "KS",
        "MO",
        "IL",
        "IN",
        "OH",
        "KY",
        "VA",
        "NC",
        "SC",
        "PA",
        "MD",
        "WV",
        "MI",
    ],
    "T2": [  # Midwest + East regional: ~18 states
        "IL",
        "IN",
        "OH",
        "MI",
        "WI",
        "MN",
        "IA",
        "MO",
        "KY",
        "TN",
        "PA",
        "NY",
        "WV",
        "VA",
        "NC",
        "KS",
        "NE",
        "SD",
    ],
    "T3": [  # West Coast + Mountain: ~14 states
        "CA",
        "OR",
        "WA",
        "NV",
        "AZ",
        "UT",
        "ID",
        "MT",
        "WY",
        "CO",
        "NM",
        "TX",
        "OK",
        "KS",
    ],
    "T4": [  # Southeast regional: ~10 states
        "GA",
        "FL",
        "AL",
        "SC",
        "NC",
        "TN",
        "MS",
        "LA",
        "KY",
        "VA",
    ],
    "T5": [  # local Ohio/Indiana: ~5 states
        "OH",
        "IN",
        "KY",
        "WV",
        "PA",
    ],
}

# Approximate state visit weighting (home states + transit routes weighted higher).
# Just a soft preference; routes still randomized.
HOME_BIAS = 3  # base-state cells get this much extra weight


# ---------------------------------------------------------------------------
# Realistic per-state fuel-tax-paid rates (USD/gal at the pump, approx
# combined retail tax). These don't have to match iftach.org exactly —
# they're what the fuel-card report would print as "tax_paid".
# ---------------------------------------------------------------------------

PUMP_TAX = {
    "AL": 0.29,
    "AR": 0.286,
    "AZ": 0.26,
    "CA": 0.97,
    "CO": 0.325,
    "FL": 0.40,
    "GA": 0.37,
    "IA": 0.325,
    "ID": 0.32,
    "IL": 0.738,
    "IN": 0.61,
    "KS": 0.26,
    "KY": 0.22,
    "LA": 0.20,
    "MA": 0.24,
    "MD": 0.46,
    "ME": 0.31,
    "MI": 0.52,
    "MN": 0.318,
    "MO": 0.295,
    "MS": 0.21,
    "MT": 0.297,
    "NC": 0.403,
    "ND": 0.23,
    "NE": 0.318,
    "NH": 0.222,
    "NJ": 0.49,
    "NM": 0.21,
    "NV": 0.27,
    "NY": 0.38,
    "OH": 0.47,
    "OK": 0.19,
    "OR": 0.0,
    "PA": 0.741,
    "RI": 0.40,
    "SC": 0.28,
    "SD": 0.28,
    "TN": 0.27,
    "TX": 0.20,
    "UT": 0.385,
    "VA": 0.327,
    "VT": 0.32,
    "WA": 0.584,
    "WI": 0.329,
    "WV": 0.357,
    "WY": 0.24,
}

# Realistic merchant names per state.
MERCHANTS = [
    "Pilot Travel Center",
    "Love's Truck Stop",
    "TA Travel Center",
    "Petro Stopping Center",
    "Flying J",
    "Sapp Bros",
    "Maverik",
    "Kwik Trip",
    "Casey's General Store",
    "QuikTrip",
]


# ---------------------------------------------------------------------------
# Date helpers — Q2 2026 = Apr 1 – Jun 30 (91 days)
# ---------------------------------------------------------------------------

Q2_START = date(2026, 4, 1)
Q2_END = date(2026, 6, 30)


def _q2_weeks_in_month(year: int, month: int) -> list[tuple[date, date]]:
    """List of (mon, sat) ranges that anchor weeks in a given month."""
    first = date(year, month, 1)
    last = (date(year, month + 1, 1) - timedelta(days=1)) if month < 12 else date(year, 12, 31)
    weeks: list[tuple[date, date]] = []
    d = first
    while d <= last:
        end = min(d + timedelta(days=6), last)
        weeks.append((d, end))
        d = end + timedelta(days=1)
    return weeks


# ---------------------------------------------------------------------------
# Per-day route generation
# ---------------------------------------------------------------------------


def _generate_day_segments(
    home_state: str, territory: list[str], target_miles: int
) -> list[tuple[str, int]]:
    """Break a day's mileage into 1–3 state segments (truck may cross borders).

    Returns list of (state, miles) for that day, summing to ~target_miles.
    """
    weights = [HOME_BIAS if s == home_state else 1 for s in territory]
    n_segments = random.choices([1, 2, 3], weights=[5, 4, 1])[0]
    states = random.choices(territory, weights=weights, k=n_segments)
    # Distribute miles across segments
    if n_segments == 1:
        return [(states[0], target_miles)]
    cuts = sorted(random.sample(range(50, target_miles - 50), n_segments - 1))
    sizes = (
        [cuts[0]] + [cuts[i] - cuts[i - 1] for i in range(1, len(cuts))] + [target_miles - cuts[-1]]
    )
    return list(zip(states, sizes, strict=True))


def generate_mileage_rows() -> list[dict]:
    """One row per truck-per-day-per-state-segment. ELD-style summary."""
    rows: list[dict] = []
    for truck_id, _card, driver, days_per_week, weeks_per_month, home in TRUCKS:
        territory = TERRITORIES[truck_id]
        for month in (4, 5, 6):
            weeks = _q2_weeks_in_month(2026, month)
            # Pick which weeks of the month this truck runs
            active_weeks = random.sample(weeks, k=min(weeks_per_month, len(weeks)))
            for week_start, week_end in active_weeks:
                week_days = (week_end - week_start).days + 1
                # Pick driving days within the week
                day_offsets = random.sample(range(week_days), k=min(days_per_week, week_days))
                weekly_hours = 0.0
                for offset in sorted(day_offsets):
                    if weekly_hours >= 70:
                        break  # HOS cap
                    d = week_start + timedelta(days=offset)
                    # Per-day miles: 450–700 (10hr * avg 55mph = ~550, cap 700)
                    daily_miles = random.randint(450, 700)
                    # Estimated drive hours (assume avg 58 mph)
                    daily_hours = round(daily_miles / 58 + random.uniform(0.5, 1.5), 1)
                    if weekly_hours + daily_hours > 70:
                        daily_hours = round(70 - weekly_hours, 1)
                        daily_miles = int(daily_hours * 58)
                    weekly_hours += daily_hours
                    if daily_miles < 100:
                        continue
                    for state, miles in _generate_day_segments(home, territory, daily_miles):
                        rows.append(
                            {
                                "date": d.isoformat(),
                                "truck_id": truck_id,
                                "driver": driver,
                                "state": state,
                                "miles": miles,
                                "hours": round(miles / 58 + random.uniform(0.1, 0.4), 2),
                            }
                        )
    return rows


# ---------------------------------------------------------------------------
# Fuel transaction generation
# ---------------------------------------------------------------------------


def generate_fuel_rows(miles_rows: list[dict]) -> list[dict]:
    """Generate fuel-card transactions correlated with the miles driven.

    Trucks fuel ~every 650 miles (typical tank ~150 gal × ~6.5 mpg ≈ 970 mi
    range; conservative refill at 650 mi). Per truck, we keep a running
    mileage tally per state of operation and emit a fuel purchase when the
    truck "needs" one. Target fleet MPG ≈ 6.5–7.0.
    """
    rows: list[dict] = []
    # Per-truck state-by-state mileage accumulator
    by_truck = sorted(miles_rows, key=lambda r: (r["truck_id"], r["date"]))
    cards = {t[0]: t[1] for t in TRUCKS}
    drivers = {t[0]: t[2] for t in TRUCKS}

    truck_state: dict[str, dict] = {
        t[0]: {"miles_since_fill": 0, "last_state": None} for t in TRUCKS
    }

    for row in by_truck:
        truck = row["truck_id"]
        state = row["state"]
        tstate = truck_state[truck]
        tstate["miles_since_fill"] += row["miles"]
        tstate["last_state"] = state

        # Buy fuel about every 600-800 miles
        threshold = random.randint(600, 800)
        if tstate["miles_since_fill"] >= threshold:
            # Mileage-since-fill * (1/mpg) → realistic gallons; vary per-truck mpg
            mpg_per_truck = {"T1": 6.6, "T2": 6.9, "T3": 7.1, "T4": 6.5, "T5": 6.3}
            mpg = mpg_per_truck[truck] + random.uniform(-0.3, 0.3)
            gallons = round(tstate["miles_since_fill"] / mpg, 2)
            tstate["miles_since_fill"] = 0
            # Pump tax rate
            tax_rate = PUMP_TAX.get(state, 0.30)
            tax_paid = round(gallons * tax_rate, 2)
            price_per_gallon = round(random.uniform(3.85, 5.45), 3)
            fuel_amount = round(gallons * price_per_gallon, 2)
            total = round(fuel_amount + (tax_paid if tax_rate > 0 else 0), 2)
            rows.append(
                {
                    "date": row["date"],
                    "card_number": cards[truck],
                    "truck_id": truck,
                    "driver": drivers[truck],
                    "merchant_name": random.choice(MERCHANTS),
                    "state": state,
                    "gallons": gallons,
                    "price_per_gallon": price_per_gallon,
                    "fuel_amount": fuel_amount,
                    "tax_paid": tax_paid,
                    "total_amount": total,
                }
            )
    return rows


# ---------------------------------------------------------------------------
# Excel writers
# ---------------------------------------------------------------------------


def write_miles_xlsx(rows: list[dict], path: Path) -> None:
    """Write a single raw-data sheet — what a real ELD export looks like."""
    df = pd.DataFrame(rows, columns=["date", "truck_id", "driver", "state", "miles", "hours"])
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="ELD Daily Summary", index=False)


def write_fuel_xlsx(rows: list[dict], path: Path) -> None:
    """Write a single raw-data sheet — what a real fuel-card export looks like."""
    df = pd.DataFrame(
        rows,
        columns=[
            "date",
            "card_number",
            "truck_id",
            "driver",
            "merchant_name",
            "state",
            "gallons",
            "price_per_gallon",
            "fuel_amount",
            "tax_paid",
            "total_amount",
        ],
    )
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="Fuel Transactions", index=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    print(f"Generating Q2 2026 test data for {CARRIER}…")
    miles = generate_mileage_rows()
    fuel = generate_fuel_rows(miles)

    miles_path = OUT_DIR / "test_logistics_miles.xlsx"
    fuel_path = OUT_DIR / "test_logistics_fuel.xlsx"
    write_miles_xlsx(miles, miles_path)
    write_fuel_xlsx(fuel, fuel_path)

    total_miles = sum(r["miles"] for r in miles)
    total_gallons = sum(r["gallons"] for r in fuel)
    mpg = total_miles / total_gallons if total_gallons else 0
    per_truck_miles: dict[str, int] = {}
    per_truck_states: dict[str, set[str]] = {}
    for r in miles:
        per_truck_miles[r["truck_id"]] = per_truck_miles.get(r["truck_id"], 0) + r["miles"]
        per_truck_states.setdefault(r["truck_id"], set()).add(r["state"])

    print(f"\n✓ {miles_path.name} — {len(miles)} mileage rows")
    print(f"✓ {fuel_path.name} — {len(fuel)} fuel transactions")
    print("\nPer-truck Q2 2026 summary:")
    print(f"{'Truck':<6}{'Miles':>10}{'States':>10}{'Driver'}")
    for truck_id, _c, driver, *_ in TRUCKS:
        m = per_truck_miles.get(truck_id, 0)
        s = len(per_truck_states.get(truck_id, set()))
        print(f"{truck_id:<6}{m:>10,}{s:>10}  {driver}")
    print(f"\nFleet totals: {total_miles:,} mi · {total_gallons:,.0f} gal · MPG {mpg:.2f}")


if __name__ == "__main__":
    main()
