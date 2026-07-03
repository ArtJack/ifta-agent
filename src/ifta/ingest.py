"""Flexible ingestion of raw owner-supplied mileage and fuel files.

The goal is to accept CSV, Excel, or PDF in whatever shape the owner sends
and produce normalized MileageRecord / FuelRecord lists.

Heuristics:
- Files are classified as "miles" or "fuel" by column header keywords.
- Inside a file we look for columns matching truck / state / miles / gallons /
  tax-paid using a tolerant name map.
- Excel files may have multiple per-truck blocks side-by-side (matching the
  existing IFTA 2025 ACTIVED.xlsx template); the parser walks the header row
  and slices each block.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import cast

import pandas as pd

from ifta.models import (
    CleanData,
    FuelRecord,
    MileageRecord,
    coalesce_fuel,
    coalesce_records,
    normalize_state,
)

MILES_KEYWORDS = {"miles", "mile", "distance", "km"}
GALLONS_KEYWORDS = {
    "gallons",
    "gallon",
    "gal",
    "fuel_volume",
    "volume",
    "qty",
    "quantity",
}
TAX_PAID_KEYWORDS = {"taxpaid", "fueltax"}
TRUCK_KEYWORDS = {"truck", "unit", "vehicle", "vin", "asset"}
STATE_KEYWORDS = {"state", "jurisdiction", "merchantstate", "buystate", "province"}
# Headers that are *exactly* a state abbreviation. Matched by equality, never as
# a substring — "st" also occurs inside cost/last/first/district. Fuel-card
# transaction exports (e.g. Love's) label the jurisdiction column simply "St".
STATE_ABBREV_HEADERS = {"st"}
DRIVER_KEYWORDS = {"driver", "operator", "drivername"}
CARD_KEYWORDS = {"cardnumber", "cardno", "fuelcard", "card"}
# A per-row product/description column lets us drop lines that aren't taxable
# IFTA diesel. DEF/AdBlue is not motor fuel; reefer/dyed diesel is off-road —
# their gallons must never land in the return.
PRODUCT_KEYWORDS = {"product"}
NONTAXABLE_PRODUCT_RE = re.compile(r"\bDEF\b|EXHAUST|REEFER|AD\s*BLUE|DYED", re.IGNORECASE)
# Columns that mark a block as already-computed summary output — we skip it.
SUMMARY_KEYWORDS = {
    "taxrate",
    "rate",
    "taxdue",
    "taxabletotalgallons",
    "nettaxable",
    "taxablefuel",
    "netgallons",
}


def _norm_header(s: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def _classify_column(header: str) -> str | None:
    h = _norm_header(header)
    if not h:
        return None
    for kw in SUMMARY_KEYWORDS:
        if kw in h:
            return "summary"
    for kw in TAX_PAID_KEYWORDS:
        if kw in h:
            return "tax_paid"
    # Card before truck/driver so "cardnumber" doesn't match "truck"/etc first.
    for kw in CARD_KEYWORDS:
        if kw in h:
            return "card"
    for kw in DRIVER_KEYWORDS:
        if kw in h:
            return "driver"
    # State before truck: "State/Province" normalizes to "stateprovince",
    # which contains "vin" and was previously misclassified as a truck/VIN.
    for kw in STATE_KEYWORDS:
        if kw in h:
            return "state"
    if h in STATE_ABBREV_HEADERS:  # bare "St" — exact match only (see set docstring)
        return "state"
    # Product/description column, so DEF/reefer rows can be dropped downstream.
    for kw in PRODUCT_KEYWORDS:
        if kw in h:
            return "product"
    for kw in TRUCK_KEYWORDS:
        if kw in h:
            return "truck"
    for kw in GALLONS_KEYWORDS:
        if kw in h:
            return "gallons"
    for kw in MILES_KEYWORDS:
        if kw in h:
            return "miles"
    return None


def classify_file(path: Path, df_columns: Iterable[str]) -> str:
    """Return 'miles', 'fuel', or 'unknown' based on filename + columns."""
    name = path.name.lower()
    if any(k in name for k in ("mile", "ifta_q", "mileage", "trip")) and "fuel" not in name:
        return "miles"
    if any(
        k in name
        for k in (
            "fuel",
            "gallons",
            "purchase",
            "ta_",
            "pilot",
            "comdata",
            "efs",
            "wex",
        )
    ):
        return "fuel"
    # fall back to column inspection
    classes = [_classify_column(c) for c in df_columns]
    has_miles = "miles" in classes
    has_gallons = "gallons" in classes
    has_tax = "tax_paid" in classes
    if has_miles and not has_gallons:
        return "miles"
    if has_gallons or has_tax:
        return "fuel"
    if has_miles:
        return "miles"
    return "unknown"


# ---------------------------------------------------------------------------
# CSV / single-table parsing
# ---------------------------------------------------------------------------


def _read_any(path: Path) -> dict[str, pd.DataFrame]:
    """Return {sheet_name: DataFrame}. CSV → single sheet keyed by 'csv'."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        # Read without using row 0 as headers — _find_header_row will pick it up.
        return {"csv": pd.read_csv(path, dtype=str, keep_default_na=False, header=None)}
    if suffix in (".xlsx", ".xlsm", ".xls"):
        xl = pd.ExcelFile(path)
        return {s: cast(pd.DataFrame, xl.parse(s, dtype=str, header=None)) for s in xl.sheet_names}
    if suffix == ".pdf":
        return _read_pdf_tables(path)
    raise ValueError(f"unsupported file type: {path}")


def _read_pdf_tables(path: Path) -> dict[str, pd.DataFrame]:
    import pdfplumber

    sheets: dict[str, pd.DataFrame] = {}
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            for j, table in enumerate(page.extract_tables() or []):
                if not table:
                    continue
                df = pd.DataFrame(table)
                sheets[f"p{i}_t{j}"] = df
    return sheets


# ---------------------------------------------------------------------------
# Per-block extraction inside a sheet
# ---------------------------------------------------------------------------


def _find_header_row(df: pd.DataFrame) -> int | None:
    """Find the row whose cells contain the most known column-classifications."""
    best_row, best_score = None, 0
    for i in range(min(len(df), 25)):
        row = df.iloc[i].tolist()
        classes = {_classify_column(c) for c in row}
        classes.discard(None)
        if len(classes) > best_score:
            best_row, best_score = i, len(classes)
    if best_score < 2:
        return None
    return best_row


def _extract_blocks(df: pd.DataFrame, header_row: int) -> list[tuple[str | None, dict[str, int]]]:
    """Slice the header row into per-truck blocks.

    Each block ends when we hit another 'truck' or 'state' column, or runs
    out of columns. Returns list of (truck_id_hint, {role: col_idx}).
    """
    headers = df.iloc[header_row].tolist()
    cols = []
    for i, h in enumerate(headers):
        role = _classify_column(h)
        cols.append((i, h, role))

    blocks: list[tuple[str | None, dict[str, int]]] = []
    current: dict[str, int] = {}
    truck_hint: str | None = None

    is_summary = [False]

    def flush() -> None:
        nonlocal current, truck_hint
        if (
            not is_summary[0]
            and current
            and ("state" in current)
            and ("miles" in current or "gallons" in current)
        ):
            blocks.append((truck_hint, current))
        current = {}
        truck_hint = None
        is_summary[0] = False

    for idx, _header, role in cols:
        if role == "state" and "state" in current:
            flush()
        if role == "summary":
            is_summary[0] = True
            continue
        if role == "truck":
            # First truck-ish column wins, so a later merchant column like
            # "Truck Stop" can't clobber the real "Truck #". (The id may also
            # come from neighbouring header text, e.g. "2013".)
            current.setdefault("truck", idx)
            continue
        if role and role not in current:
            current[role] = idx
            # capture the year-ish truck hint from neighbouring header text
            if role == "state" and truck_hint is None:
                # look left for a numeric year header (e.g., '2013')
                for back_i in range(idx - 1, max(idx - 3, -1), -1):
                    val = str(headers[back_i] if back_i < len(headers) else "").strip()
                    if val.isdigit() and 1990 <= int(val[:4]) <= 2099:
                        truck_hint = val
                        break
    flush()
    return blocks


def _to_float(v: object) -> float:
    if v is None:
        return 0.0
    s = str(v).strip().replace(",", "").replace("$", "")
    if s in ("", "-", "—", "nan", "NaN"):
        return 0.0
    # parentheses = negative (accounting style)
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        f = float(s)
    except ValueError:
        return 0.0
    return -f if neg else f


def _cell_str(row: pd.Series, col: int) -> str | None:
    """Read a single cell as a non-empty stripped string, else None."""
    if col is None or col >= len(row):
        return None
    v = row.iloc[col]
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    return s


def _first_nonempty_cell(row: pd.Series) -> str:
    for value in row.tolist():
        text = str(value).strip()
        if text and text.lower() != "nan":
            return text
    return ""


def _rows_from_block(
    df: pd.DataFrame,
    header_row: int,
    truck_hint: str | None,
    roles: dict[str, int],
) -> tuple[list[MileageRecord], list[FuelRecord], dict[str, str], dict[str, str]]:
    """Extract records from one block.

    Returns (miles, fuel, drivers_by_truck, cards_by_truck).
    """
    miles: list[MileageRecord] = []
    fuel: list[FuelRecord] = []
    drivers: dict[str, str] = {}
    cards: dict[str, str] = {}

    state_col = roles["state"]
    truck_col = roles.get("truck")
    miles_col = roles.get("miles")
    gallons_col = roles.get("gallons")
    tax_col = roles.get("tax_paid")
    driver_col = roles.get("driver")
    card_col = roles.get("card")
    product_col = roles.get("product")

    for r in range(header_row + 1, len(df)):
        row = df.iloc[r]
        if _first_nonempty_cell(row).upper() == "TOTAL":
            continue
        state = normalize_state(row.iloc[state_col] if state_col < len(row) else None)
        if not state:
            continue
        truck_id = truck_hint or "unknown"
        if truck_col is not None and truck_col < len(row):
            v = str(row.iloc[truck_col]).strip()
            if v and v.lower() != "nan":
                truck_id = v

        # Capture driver / card as truck-level lookups (first non-empty wins).
        if driver_col is not None:
            d = _cell_str(row, driver_col)
            if d and truck_id not in drivers:
                drivers[truck_id] = d
        if card_col is not None:
            c = _cell_str(row, card_col)
            if c and truck_id not in cards:
                cards[truck_id] = c

        if miles_col is not None and miles_col < len(row):
            m = _to_float(row.iloc[miles_col])
            if m:
                miles.append(MileageRecord(truck_id, state, m))

        # A DEF/AdBlue/reefer line is not taxable IFTA fuel — count no gallons
        # and claim no tax-paid credit for it, regardless of the amounts shown.
        nontaxable = False
        if product_col is not None and product_col < len(row):
            pv = row.iloc[product_col]
            if pv is not None and NONTAXABLE_PRODUCT_RE.search(str(pv)):
                nontaxable = True

        if not nontaxable:
            if gallons_col is not None and gallons_col < len(row):
                g = _to_float(row.iloc[gallons_col])
                tp = (
                    _to_float(row.iloc[tax_col])
                    if tax_col is not None and tax_col < len(row)
                    else 0.0
                )
                if g or tp:
                    fuel.append(FuelRecord(truck_id, state, g, tp))
            elif tax_col is not None and tax_col < len(row):
                tp = _to_float(row.iloc[tax_col])
                if tp:
                    fuel.append(FuelRecord(truck_id, state, 0.0, tp))
    return miles, fuel, drivers, cards


def parse_sheet(
    df: pd.DataFrame, default_truck: str | None = None
) -> tuple[list[MileageRecord], list[FuelRecord], dict[str, str], dict[str, str]]:
    """Extract miles, fuel, drivers, cards from one DataFrame."""
    if df.empty:
        return [], [], {}, {}
    hr = _find_header_row(df)
    if hr is None:
        return [], [], {}, {}
    blocks = _extract_blocks(df, hr)
    miles: list[MileageRecord] = []
    fuel: list[FuelRecord] = []
    drivers: dict[str, str] = {}
    cards: dict[str, str] = {}
    for hint, roles in blocks:
        m, f, d, c = _rows_from_block(df, hr, hint or default_truck, roles)
        miles.extend(m)
        fuel.extend(f)
        # First-write-wins across blocks for the same truck.
        for k, v in d.items():
            drivers.setdefault(k, v)
        for k, v in c.items():
            cards.setdefault(k, v)
    return miles, fuel, drivers, cards


def ingest_file(path: Path) -> CleanData:
    """Read one raw file and return normalized records.

    Records are NOT deduplicated here; callers merge across files first.
    """
    sheets = _read_any(path)
    out = CleanData()
    for _, df in sheets.items():
        m, f, d, c = parse_sheet(df)
        out.miles.extend(m)
        out.fuel.extend(f)
        for k, v in d.items():
            out.truck_drivers.setdefault(k, v)
        for k, v in c.items():
            out.truck_cards.setdefault(k, v)
    return out


def ingest_folder(folder: Path, *, skip_files: set[str] | None = None) -> CleanData:
    """Read every supported file in a folder and merge.

    `skip_files`, when given, names files (by basename) to skip — used by
    the web pipeline to honor preflight's auto-dedup decisions for files
    that look like duplicate summary/detail exports of the same data.
    """
    skip = skip_files or set()
    merged = CleanData()
    for path in sorted(folder.iterdir()):
        if path.is_dir() or path.name.startswith("."):
            continue
        if path.suffix.lower() not in (".csv", ".xlsx", ".xlsm", ".xls", ".pdf"):
            continue
        if path.name in skip:
            continue
        try:
            data = ingest_file(path)
        except Exception as e:
            print(f"  ! skipping {path.name}: {e}")
            continue
        if not data.miles and not data.fuel:
            # A recognised data file that yields nothing is almost always an
            # unrecognised layout (e.g. a jurisdiction column not named
            # State/St/Jurisdiction) — surface it loudly instead of silently
            # dropping a whole fuel or mileage source from the return.
            print(
                f"  ⚠ {path.name}: no mileage or fuel rows recognised — this file "
                "contributed NOTHING to the return. Check its column headers "
                "(especially the jurisdiction/state column)."
            )
        merged.miles.extend(data.miles)
        merged.fuel.extend(data.fuel)
        for k, v in data.truck_drivers.items():
            merged.truck_drivers.setdefault(k, v)
        for k, v in data.truck_cards.items():
            merged.truck_cards.setdefault(k, v)
    merged.miles = coalesce_records(merged.miles)
    merged.fuel = coalesce_fuel(merged.fuel)
    return merged
