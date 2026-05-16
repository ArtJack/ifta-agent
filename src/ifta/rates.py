"""Fetch IFTA tax-rate matrix from iftach.org.

Downloads the CSV at /taxmatrix/charts/<NQ20YY>.csv, parses each
jurisdiction's diesel rate (USD), and caches the result on disk so we don't
hit the network on every run.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path

import requests

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "rates"
BASE_URL = "https://www.iftach.org/taxmatrix/charts"


# Map IFTA jurisdiction names → 2-letter postal codes.
JURISDICTION_MAP: dict[str, str] = {
    # Canadian provinces
    "ALBERTA": "AB",
    "BRITISH COLUMBIA": "BC",
    "MANITOBA": "MB",
    "NEW BRUNSWICK": "NB",
    "NEWFOUNDLAND": "NL",
    "NEWFOUNDLAND AND LABRADOR": "NL",
    "NOVA SCOTIA": "NS",
    "ONTARIO": "ON",
    "PRINCE EDWARD ISLAND": "PE",
    "QUEBEC": "QC",
    "SASKATCHEWAN": "SK",
    # US states
    "ALABAMA": "AL",
    "ARIZONA": "AZ",
    "ARKANSAS": "AR",
    "CALIFORNIA": "CA",
    "COLORADO": "CO",
    "CONNECTICUT": "CT",
    "DELAWARE": "DE",
    "FLORIDA": "FL",
    "GEORGIA": "GA",
    "IDAHO": "ID",
    "ILLINOIS": "IL",
    "INDIANA": "IN",
    "IOWA": "IA",
    "KANSAS": "KS",
    "KENTUCKY": "KY",
    "LOUISIANA": "LA",
    "MAINE": "ME",
    "MARYLAND": "MD",
    "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI",
    "MINNESOTA": "MN",
    "MISSISSIPPI": "MS",
    "MISSOURI": "MO",
    "MONTANA": "MT",
    "NEBRASKA": "NE",
    "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM",
    "NEW YORK": "NY",
    "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND",
    "OHIO": "OH",
    "OKLAHOMA": "OK",
    "OREGON": "OR",
    "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN",
    "TEXAS": "TX",
    "UTAH": "UT",
    "VERMONT": "VT",
    "VIRGINIA": "VA",
    "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI",
    "WYOMING": "WY",
}

# Column index of "Special Diesel" rate in the matrix CSV (0-based, after
# jurisdiction + currency cols).  Header order:
# Gasoline(0) Special-Diesel(1) Gasohol(2) Propane(3) LNG(4) CNG(5) Ethanol(6)
# Methanol(7) E-85(8) M-85(9) A55(10) Biodiesel(11) Electricity(12)
# Hydrogen(13) Hythane(14)
FUEL_COLUMNS: dict[str, int] = {
    "gasoline": 0,
    "diesel": 1,
    "special_diesel": 1,
    "gasohol": 2,
    "propane": 3,
    "lng": 4,
    "cng": 5,
    "ethanol": 6,
    "methanol": 7,
    "e85": 8,
    "m85": 9,
    "a55": 10,
    "biodiesel": 11,
    "electricity": 12,
    "hydrogen": 13,
    "hythane": 14,
}


@dataclass
class RateTable:
    quarter: str  # e.g. "1Q2026"
    fuel: str  # e.g. "diesel"
    rates: dict[str, float]  # state code → base USD per gallon
    surcharge_rates: dict[
        str, float
    ]  # state code → surcharge USD per gallon (only non-zero entries)
    requested_quarter: str | None = None
    source_quarter: str | None = None
    fallback_used: bool = False
    warning: str | None = None

    def __post_init__(self) -> None:
        if self.requested_quarter is None:
            self.requested_quarter = self.quarter
        if self.source_quarter is None:
            self.source_quarter = self.quarter

    def get(self, state: str, default: float = 0.0) -> float:
        return self.rates.get(state.upper(), default)

    def surcharge(self, state: str) -> float:
        return self.surcharge_rates.get(state.upper(), 0.0)

    def has_surcharge(self, state: str) -> bool:
        return self.surcharge_rates.get(state.upper(), 0.0) > 0


def _quarter_url(quarter: str) -> tuple[str, str]:
    # accept "1Q2026" / "Q1-2026" / "Q1 2026" / "Q1_2026"
    s = quarter.strip().upper().replace(" ", "").replace("-", "").replace("_", "")
    m = re.match(r"^Q?(\d)Q?(\d{4})$", s)
    if not m:
        raise ValueError(f"unrecognised quarter: {quarter}")
    q, y = m.group(1), m.group(2)
    return f"{BASE_URL}/{q}Q{y}.csv", f"{q}Q{y}"


def _strip_money(s: str) -> float:
    s = s.replace("$", "").replace(",", "").strip()
    if s in ("", "-", "—"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


SURCHARGE_PATTERN = re.compile(r"\bsur\s*chg\b|\bsurcharge\b", re.IGNORECASE)


def _canonical_name(raw: str) -> tuple[str | None, bool]:
    """Return (state_code, is_surcharge_row)."""
    is_surcharge = bool(SURCHARGE_PATTERN.search(raw))
    name = re.sub(r"#\s*\d+", "", raw)
    name = SURCHARGE_PATTERN.sub("", name)
    name = name.strip().upper()
    if not name:
        return None, is_surcharge
    return JURISDICTION_MAP.get(name), is_surcharge


def _previous_quarter(qkey: str) -> str:
    """E.g. '2Q2026' -> '1Q2026'; '1Q2026' -> '4Q2025'."""
    q, y = int(qkey[0]), int(qkey[2:])
    if q == 1:
        return f"4Q{y - 1}"
    return f"{q - 1}Q{y}"


def fetch_rates(quarter: str, fuel: str = "diesel", *, force: bool = False) -> RateTable:
    """Fetch a quarter's IFTA rate matrix, with graceful fallback.

    If the requested quarter isn't published on iftach.org yet (common
    early in a new quarter), falls back to the most recent published
    quarter and prints a warning. The returned RateTable still carries
    the requested `quarter` label so downstream output is consistent.
    """
    requested_url, qkey = _quarter_url(quarter)
    fuel_col = FUEL_COLUMNS.get(fuel.lower().replace(" ", "_"))
    if fuel_col is None:
        raise ValueError(f"unknown fuel: {fuel}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{qkey}.csv"
    source_qkey = qkey
    warning: str | None = None
    if force or not cache_path.exists():
        try:
            resp = requests.get(requested_url, timeout=30)
            resp.raise_for_status()
            cache_path.write_bytes(resp.content)
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", None) != 404:
                raise
            # 404 — quarter not published yet. Walk backward up to 3 quarters
            # looking for a cached or fetchable matrix.
            fallback = _previous_quarter(qkey)
            for _ in range(3):
                fb_path = CACHE_DIR / f"{fallback}.csv"
                if fb_path.exists():
                    print(f"  ⚠ {qkey} not published yet — falling back to cached {fallback}")
                    cache_path = fb_path
                    source_qkey = fallback
                    break
                fb_url, _ = _quarter_url(fallback)
                try:
                    resp = requests.get(fb_url, timeout=30)
                    resp.raise_for_status()
                    fb_path.write_bytes(resp.content)
                    print(f"  ⚠ {qkey} not published yet — fetched {fallback} instead")
                    cache_path = fb_path
                    source_qkey = fallback
                    break
                except requests.HTTPError:
                    fallback = _previous_quarter(fallback)
            else:
                raise RuntimeError(
                    f"No IFTA rate matrix available for {qkey} or the 3 prior quarters."
                ) from e

    if source_qkey != qkey:
        warning = (
            f"{qkey} IFTA rates were not published, so calculations used {source_qkey} "
            "rates. Do not file until the current-quarter rate matrix is confirmed."
        )

    raw = cache_path.read_text(encoding="utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(raw))
    rates: dict[str, float] = {}
    surcharges: dict[str, float] = {}
    pending_state: str | None = None
    pending_is_surcharge: bool = False
    for row in reader:
        if not row or all(not c.strip() for c in row):
            pending_state = None
            pending_is_surcharge = False
            continue
        first = row[0].strip()
        currency = row[1].strip() if len(row) > 1 else ""
        if first:
            pending_state, pending_is_surcharge = _canonical_name(first)
        if not pending_state:
            continue
        if currency.upper() != "U.S.":
            continue
        if len(row) <= 2 + fuel_col:
            continue
        rate = _strip_money(row[2 + fuel_col])
        if rate > 0:
            if pending_is_surcharge:
                surcharges[pending_state] = rate
            else:
                rates[pending_state] = rate
    return RateTable(
        quarter=qkey,
        fuel=fuel,
        rates=rates,
        surcharge_rates=surcharges,
        requested_quarter=qkey,
        source_quarter=source_qkey,
        fallback_used=source_qkey != qkey,
        warning=warning,
    )
