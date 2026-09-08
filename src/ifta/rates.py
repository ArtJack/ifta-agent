"""Fetch IFTA tax-rate matrix from iftach.org.

Downloads the CSV at /taxmatrix/charts/<NQ20YY>.csv, parses each
jurisdiction's diesel rate (USD), and caches the result on disk so we don't
hit the network on every run.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import requests

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "rates"
BASE_URL = "https://www.iftach.org/taxmatrix/charts"

# A rate fallback is an operational event the worker and journal must record:
# it means a filing was priced from a prior quarter. print() went to a stdout
# nobody reads in production.
log = logging.getLogger("ifta.rates")


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


def _strip_money(s: str) -> float | None:
    """Parse one rate cell. Returns None when the cell cannot be read.

    "No rate" and "we could not read the rate" are different facts and must not
    share a representation. An empty or dashed cell is a real zero (Oregon
    charges a weight-mile tax instead; Indiana publishes no diesel surcharge).
    Anything else that will not parse is *unknown* — and returning 0.0 for it
    meant a jurisdiction quietly dropped out of the matrix and every mile driven
    there computed $0.00 of tax on a government filing.
    """
    s = s.replace("$", "").replace(",", "").strip()
    if s in ("", "-", "—"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return None


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


class RateMatrixInvalidError(Exception):
    """A fetched response didn't parse as a plausible IFTA rate matrix."""


# A real IFTA matrix carries every member jurisdiction (58 US states/provinces
# in the current agreement; the seeded files parse to 57 priced rows for
# diesel, Oregon being intentionally rate-free). A maintenance/WAF HTML page or
# a truncated CSV parses to far fewer. Anything under this floor is rejected
# rather than cached, because `cache_path.exists()` short-circuits every later
# fetch — one bad response would otherwise poison the quarter permanently and
# compute $0 tax for the missing jurisdictions.
_MIN_PLAUSIBLE_JURISDICTIONS = 50

# The diesel column. A row too short to reach *this* column is malformed; a row
# too short to reach a later one simply means that jurisdiction doesn't publish
# a rate for that alternative fuel.
_CORE_FUEL_COL = 1


def _is_404(exc: Exception) -> bool:
    return (
        isinstance(exc, requests.HTTPError)
        and getattr(exc.response, "status_code", None) == 404
    )


def _parse_matrix(
    raw: str, fuel_col: int
) -> tuple[dict[str, float], dict[str, float], list[str], int]:
    """Parse a rate-matrix CSV into (base_rates, surcharges, unparseable, seen).

    ``unparseable`` names the jurisdictions whose rate cell was present but
    unreadable (or whose row was truncated). Those are the dangerous ones: they
    are absent from ``rates``, which is indistinguishable from "not an IFTA
    member" unless the caller is told. See :func:`_check_matrix`.

    ``seen`` counts the distinct jurisdictions the file *describes*, whether or
    not they tax this fuel. That, not the number of priced rows, is what says
    "this is an IFTA matrix rather than an error page" — most jurisdictions
    publish no hydrogen or electricity rate at all.
    """
    reader = csv.reader(io.StringIO(raw))
    rates: dict[str, float] = {}
    surcharges: dict[str, float] = {}
    unparseable: list[str] = []
    seen: set[str] = set()
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
        if not pending_is_surcharge:
            seen.add(pending_state)
        label = f"{pending_state} surcharge" if pending_is_surcharge else pending_state
        if len(row) <= 2 + fuel_col:
            # A row too short to reach this fuel's column means the jurisdiction
            # publishes nothing for it, which is ordinary for the alternative
            # fuels. Only a row short of the *diesel* column is malformed.
            if fuel_col > _CORE_FUEL_COL:
                continue
            unparseable.append(label)
            continue
        rate = _strip_money(row[2 + fuel_col])
        if rate is None:
            unparseable.append(label)
            continue
        if rate > 0:
            if pending_is_surcharge:
                surcharges[pending_state] = rate
            else:
                rates[pending_state] = rate
    return rates, surcharges, unparseable, len(seen)


def _check_matrix(
    rates: dict[str, float], unparseable: list[str], seen: int, *, source: str
) -> None:
    """Raise unless a parsed matrix is fit to compute a tax filing from.

    Applied to cached files as well as fresh downloads. ``cache_path.exists()``
    short-circuits every later fetch, so a matrix that was poisoned once would
    otherwise stay authoritative for the rest of the quarter.

    The floor counts jurisdictions *described*, not jurisdictions priced for the
    requested fuel. Counting priced rows made this a diesel-only check: propane
    is taxed by 47 jurisdictions and hydrogen by about 13, so a >= 50 floor
    rejected a perfectly valid matrix for most of the fifteen supported fuels.
    """
    if unparseable:
        raise RateMatrixInvalidError(
            f"{source} has unreadable rate cells for: {', '.join(unparseable)}. "
            "Refusing to price a filing from a matrix with unknown rates — "
            "re-fetch with `ifta rates --quarter <Q> --force`."
        )
    if seen < _MIN_PLAUSIBLE_JURISDICTIONS:
        raise RateMatrixInvalidError(
            f"{source} describes {seen} jurisdictions "
            f"(expected >= {_MIN_PLAUSIBLE_JURISDICTIONS}) — refusing to use it. "
            "A maintenance or WAF page parses like this."
        )
    if not rates:
        raise RateMatrixInvalidError(
            f"{source} priced no jurisdictions at all for this fuel — refusing to use it."
        )


def _download_matrix(url: str, dest: Path, fuel_col: int) -> None:
    """Fetch a rate matrix and cache it only if it parses as a real one."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    raw = resp.content.decode("utf-8-sig", errors="replace")
    rates, _, unparseable, seen = _parse_matrix(raw, fuel_col)
    _check_matrix(rates, unparseable, seen, source=url)
    dest.write_bytes(resp.content)


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
            _download_matrix(requested_url, cache_path, fuel_col)
        except (requests.RequestException, RateMatrixInvalidError) as e:
            # The requested quarter is unusable — not published yet (404), the
            # site is unreachable, or it served something that isn't a rate
            # matrix. Any of these must degrade to the prior quarter rather
            # than crash: the caller still gets a table, flagged DO_NOT_FILE
            # via `warning`. (Previously only a 404 was handled, so an
            # iftach.org outage failed the whole submission even with a
            # perfectly good cached quarter sitting on disk.)
            reason = "not published yet" if _is_404(e) else f"unavailable ({e})"
            fallback = _previous_quarter(qkey)
            for _ in range(3):
                fb_path = CACHE_DIR / f"{fallback}.csv"
                if fb_path.exists():
                    log.warning("%s %s — falling back to cached %s", qkey, reason, fallback)
                    cache_path = fb_path
                    source_qkey = fallback
                    break
                fb_url, _ = _quarter_url(fallback)
                try:
                    _download_matrix(fb_url, fb_path, fuel_col)
                    log.warning("%s %s — fetched %s instead", qkey, reason, fallback)
                    cache_path = fb_path
                    source_qkey = fallback
                    break
                except (requests.RequestException, RateMatrixInvalidError):
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
    rates, surcharges, unparseable, seen = _parse_matrix(raw, fuel_col)
    _check_matrix(
        rates, unparseable, seen, source=f"{source_qkey} rate cache ({cache_path})"
    )
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
