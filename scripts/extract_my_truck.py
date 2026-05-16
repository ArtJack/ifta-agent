"""Extract MENSHIKOV LLC's historical CDTFA filings from PDFs.

Reads:
    ../MyTruck/*.pdf   (CDTFA Online Services filing-record PDFs)

Writes:
    data/my_truck_history.json   per-quarter structured data
    data/my_truck_profile.json   derived operating profile

The LLC is retired — this data is kept as a reference example of a real,
filed IFTA Quarterly Return.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from pathlib import Path

import pdfplumber

MY_TRUCK = Path(__file__).resolve().parents[1].parent / "MyTruck"
OUT_HISTORY = Path("data/clients/menshikov_llc/history.json")
OUT_PROFILE = Path("data/clients/menshikov_llc/profile.json")


# ---------------------------------------------------------------------------
# Filename → (quarter, year)
# ---------------------------------------------------------------------------


def parse_quarter_from_name(name: str) -> tuple[str, str] | None:
    """E.g. '4Q25 IFTA.pdf' → ('Q4', '2025'); '1st Qt. 2024' → ('Q1', '2024')."""
    m = re.search(r"(\d)Q(\d{2,4})", name)
    if m:
        q, y = m.group(1), m.group(2)
        if len(y) == 2:
            y = "20" + y
        return f"Q{q}", y
    m = re.search(r"(1st|2nd|3rd|4th)\s*Qt\.?\s*(\d{4})", name, re.IGNORECASE)
    if m:
        mapping = {"1st": "Q1", "2nd": "Q2", "3rd": "Q3", "4th": "Q4"}
        return mapping[m.group(1).lower()], m.group(2)
    return None


# ---------------------------------------------------------------------------
# PDF → structured filing dict
# ---------------------------------------------------------------------------


# Line shape: "Arizona 2. Diesel 2,728 2,728 7.31 373 789 -416 0.2600 -$108.16 $0.00 -$108.16"
LINE_PATTERN = re.compile(
    r"^([A-Za-z][A-Za-z\s]+?)\s+(Surcharge\s+)?(\d+\.\s*\w+)"
    r"\s+([\d,]+)\s+([\d,]+)\s+([\d.]+)"
    r"\s+([\d,]+)\s+([\d,]+)\s+(-?[\d,]+)"
    r"\s+(-?[\d.]+)\s+(-?\$?[\d,.]+)",
    re.MULTILINE,
)


def _grab(pattern: str, text: str, default: str | None = None) -> str | None:
    m = re.search(pattern, text)
    return m.group(1) if m else default


def _to_float(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def parse_filing(path: Path) -> dict:
    quarter, year = parse_quarter_from_name(path.name) or (None, None)
    with pdfplumber.open(path) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)

    jurisdictions = []
    total_miles = 0
    fleet_mpg: float | None = None
    for m in LINE_PATTERN.finditer(text):
        name = m.group(1).strip()
        is_surcharge = bool(m.group(2))
        miles = int(m.group(4).replace(",", ""))
        mpg = float(m.group(6))
        if not is_surcharge:
            total_miles += miles
            if fleet_mpg is None or mpg > 0:
                fleet_mpg = mpg
        jurisdictions.append(
            {
                "name": name,
                "is_surcharge": is_surcharge,
                "miles": miles,
                "taxable_miles": int(m.group(5).replace(",", "")),
                "mpg": mpg,
                "taxable_gal": int(m.group(7).replace(",", "")),
                "tax_paid_gal": int(m.group(8).replace(",", "")),
                "net_taxable_gal": int(m.group(9).replace(",", "")),
                "rate": float(m.group(10)),
                "tax": m.group(11).replace("$", "").replace(",", ""),
            }
        )

    return {
        "quarter": quarter,
        "year": year,
        "file": path.name,
        "account": _grab(r"Account Number[:\s]*([\d-]+)", text),
        "period_begin": _grab(r"Period Begin:\s*([A-Za-z]+\s+\d+,\s+\d{4})", text),
        "period_end": _grab(r"Period End:\s*([A-Za-z]+\s+\d+,\s+\d{4})", text),
        "due_date": _grab(r"Due Date:\s*([A-Za-z]+\s+\d+,\s+\d{4})", text),
        "submitted": _grab(r"Submitted:\s*(\S+\s+\d+:\d+:\d+\s*[AP]M)", text),
        "confirmation": _grab(r"Confirmation #:\s*([\d-]+)", text),
        "subtotal": _to_float(_grab(r"Subtotal Amount Due or Credit\s*\$?([-\d,.]+)", text)),
        "penalty": _to_float(_grab(r"Penalty\s*\$?([-\d,.]+)", text)),
        "interest": _to_float(_grab(r"Interest\s*\$?([-\d,.]+)", text)),
        "total_due_or_credit": _to_float(
            _grab(r"TOTAL BALANCE DUE OR CREDIT\s*\$?([-\d,.]+)", text)
        ),
        "total_miles": total_miles or None,
        "fleet_mpg": fleet_mpg,
        "jurisdictions": jurisdictions,
    }


# ---------------------------------------------------------------------------
# History → operating profile
# ---------------------------------------------------------------------------


def build_profile(history: list[dict]) -> dict:
    filings = [f for f in history if f.get("total_due_or_credit") is not None]
    mpgs = [f["fleet_mpg"] for f in filings if f["fleet_mpg"]]
    miles = [f["total_miles"] for f in filings if f["total_miles"]]
    totals = [f["total_due_or_credit"] for f in filings]

    state_count: Counter = Counter()
    state_miles: Counter = Counter()
    state_with_surcharge: set[str] = set()
    surcharge_quarters = 0

    for f in filings:
        had_surcharge = False
        for j in f.get("jurisdictions", []):
            name = j["name"]
            if "Jurisdiction" in name or "\n" in name:
                continue  # PDF header artifact
            if j["is_surcharge"]:
                state_with_surcharge.add(name)
                had_surcharge = True
            else:
                state_count[name] += 1
                state_miles[name] += j["miles"]
        if had_surcharge:
            surcharge_quarters += 1

    n = len(filings)
    return {
        "operator": "MENSHIKOV LLC (RETIRED)",
        "account_number": "238-929600",
        "base_state": "California",
        "portal": "CDTFA online services (https://onlineservices.cdtfa.ca.gov/)",
        "support_phone": "1-800-400-7115",
        "fleet": {"trucks": 1, "truck_id": "800", "fuel_type": "Diesel"},
        "history_window": {
            "first_quarter": "Q4 2021",
            "last_quarter": "Q4 2025",
            "filings_parsed": n,
            "missing": "Q3 2024 (scanned image PDF — no extractable text)",
        },
        "fleet_mpg_stats": {
            "min": round(min(mpgs), 2),
            "max": round(max(mpgs), 2),
            "mean": round(statistics.mean(mpgs), 2),
            "median": round(statistics.median(mpgs), 2),
            "stdev": round(statistics.stdev(mpgs), 2),
        },
        "quarterly_miles_stats": {
            "min": min(miles),
            "max": max(miles),
            "mean": round(statistics.mean(miles)),
            "median": round(statistics.median(miles)),
        },
        "tax_due_stats": {
            "min": round(min(totals), 2),
            "max": round(max(totals), 2),
            "mean": round(statistics.mean(totals), 2),
            "median": round(statistics.median(totals), 2),
        },
        "routes": {
            "always_or_almost_always_visited": sorted(
                s for s, c in state_count.items() if c >= n * 0.75
            ),
            "frequently_visited": sorted(s for s, c in state_count.items() if 5 <= c < n * 0.75),
            "occasionally_visited": sorted(s for s, c in state_count.items() if 2 <= c < 5),
            "rare_one_off": sorted(s for s, c in state_count.items() if c == 1),
            "top_10_by_total_miles": [
                {"state": s, "total_miles_over_history": m}
                for s, m in sorted(state_miles.items(), key=lambda kv: -kv[1])[:10]
            ],
        },
        "surcharge_history": {
            "states_with_surcharge_lines": sorted(state_with_surcharge),
            "quarters_with_surcharge_lines": surcharge_quarters,
        },
        "patterns_and_flags": [
            "California is the single largest revenue contributor every quarter.",
            "Negative net taxable gallons are NORMAL for AZ, NM, NV, WY.",
            "Oregon: 200–2,300 miles per quarter at $0 IFTA rate.",
            "KY and VA surcharge lines appear only when those states had taxable miles.",
            "Q4 2024 anomaly: 6,670 miles, $184 tax — likely truck downtime.",
        ],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if not MY_TRUCK.exists():
        raise SystemExit(f"MyTruck/ folder not found at {MY_TRUCK}")

    history: list[dict] = []
    for pdf in sorted(MY_TRUCK.glob("*.pdf")):
        print(f"Parsing {pdf.name}…")
        try:
            history.append(parse_filing(pdf))
        except Exception as e:
            print(f"  ! skipping: {e}")

    OUT_HISTORY.write_text(json.dumps(history, indent=2))
    OUT_PROFILE.write_text(json.dumps(build_profile(history), indent=2))

    print(f"\nWrote {len(history)} filings → {OUT_HISTORY}")
    print(f"Wrote profile          → {OUT_PROFILE}")


if __name__ == "__main__":
    main()
