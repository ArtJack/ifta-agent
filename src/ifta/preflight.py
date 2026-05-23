"""Deterministic pre-compute checks on raw inbox files.

Validator catches issues in the COMPUTED return (MPG out of range, fuel
without miles, etc.). Preflight catches issues in the RAW INPUTS before
compute runs — missing files, unparseable formats, truck IDs that don't
reconcile between miles and fuel, etc. Cheap, fast, deterministic.

Findings flow into `ifta deliver` so a structurally-broken inbox doesn't
silently produce a wrong portal CSV. Agent can also call
`inspect_raw_inputs` to see the same data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ifta.ingest import ingest_file, ingest_folder
from ifta.validator import load_kb

SUPPORTED_SUFFIXES = {".csv", ".xlsx", ".xlsm", ".xls", ".pdf"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".tif", ".tiff"}
PreflightSeverity = Literal["error", "warning", "info"]


@dataclass
class PreflightFinding:
    severity: PreflightSeverity
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "code": self.code, "message": self.message}


@dataclass
class FilePreview:
    name: str
    suffix: str
    size_bytes: int
    note: str = ""  # short description (e.g. "Excel, sheets: X, Y")
    parsed_mile_rows: int = 0
    parsed_fuel_rows: int = 0
    parsed_miles_total: float = 0.0
    parsed_fuel_total: float = 0.0


@dataclass
class PreflightReport:
    inbox: Path
    files: list[FilePreview] = field(default_factory=list)
    findings: list[PreflightFinding] = field(default_factory=list)

    # Quick-roll-up stats from ingest, useful for the agent + CLI display.
    trucks_in_miles: list[str] = field(default_factory=list)
    trucks_in_fuel: list[str] = field(default_factory=list)
    mile_rows: int = 0
    fuel_rows: int = 0
    total_miles: float = 0.0
    total_gallons: float = 0.0
    raw_mpg: float = 0.0
    drivers: dict[str, str] = field(default_factory=dict)
    cards: dict[str, str] = field(default_factory=dict)

    @property
    def has_errors(self) -> bool:
        return any(f.severity == "error" for f in self.findings)

    def to_dict(self) -> dict[str, object]:
        return {
            "inbox": str(self.inbox),
            "files": [
                {
                    "name": f.name,
                    "suffix": f.suffix,
                    "size_bytes": f.size_bytes,
                    "note": f.note,
                    "parsed_mile_rows": f.parsed_mile_rows,
                    "parsed_fuel_rows": f.parsed_fuel_rows,
                    "parsed_miles_total": f.parsed_miles_total,
                    "parsed_fuel_total": f.parsed_fuel_total,
                }
                for f in self.files
            ],
            "trucks_in_miles": self.trucks_in_miles,
            "trucks_in_fuel": self.trucks_in_fuel,
            "mile_rows": self.mile_rows,
            "fuel_rows": self.fuel_rows,
            "total_miles": self.total_miles,
            "total_gallons": self.total_gallons,
            "raw_mpg": self.raw_mpg,
            "drivers": self.drivers,
            "cards": self.cards,
            "findings": [f.to_dict() for f in self.findings],
        }


def _peek_file(path: Path) -> FilePreview:
    """One-line description per file. Doesn't crack the full file open."""
    suffix = path.suffix.lower()
    size = path.stat().st_size
    note = ""
    try:
        if suffix in (".xlsx", ".xlsm"):
            import openpyxl  # local import: openpyxl is heavy

            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            note = f"sheets: {', '.join(wb.sheetnames)}"
            wb.close()
        elif suffix == ".xls":
            # Old binary Excel format — openpyxl can't read it, so the preview
            # used to print a scary "couldn't preview" warning even though the
            # ingester parses .xls fine via xlrd. Use xlrd here too, mirroring
            # the ingester, so the preview agrees with reality.
            import xlrd  # local import: xlrd is heavy

            wb = xlrd.open_workbook(str(path), on_demand=True)
            try:
                note = f"sheets: {', '.join(wb.sheet_names())}"
            finally:
                wb.release_resources()
        elif suffix == ".csv":
            with path.open(encoding="utf-8", errors="replace") as fh:
                header = fh.readline().strip()
            note = f"header: {header[:120]}"
        elif suffix == ".pdf":
            note = "pdf"
        elif suffix in IMAGE_SUFFIXES:
            note = "image/receipt candidate"
    except Exception as e:
        note = f"couldn't preview: {e}"
    return FilePreview(name=path.name, suffix=suffix, size_bytes=size, note=note)


def preflight_inputs(inbox: Path) -> PreflightReport:
    """Run deterministic checks against a quarter's inbox folder."""
    report = PreflightReport(inbox=inbox)

    # ---- 1. Folder + files exist ----
    if not inbox.exists():
        report.findings.append(
            PreflightFinding(
                "error",
                "INBOX_MISSING",
                f"Inbox folder does not exist: {inbox}",
            )
        )
        return report

    files = [
        p
        for p in sorted(inbox.iterdir())
        if p.is_file() and not p.name.startswith(".") and p.name != "client.json"
    ]
    if not files:
        report.findings.append(
            PreflightFinding("error", "INBOX_EMPTY", f"No raw files found in {inbox}.")
        )
        return report

    # ---- 2. Each file is a supported or known-reference format ----
    for p in files:
        preview = _peek_file(p)
        report.files.append(preview)
        if preview.suffix in IMAGE_SUFFIXES:
            report.findings.append(
                PreflightFinding(
                    "warning",
                    "RECEIPT_IMAGE_UNPARSED",
                    f"{p.name} looks like a receipt/image. It is saved as reference, "
                    "but image receipts are not included in mileage or gallons unless "
                    "they are converted to structured fuel data.",
                )
            )
        elif preview.suffix not in SUPPORTED_SUFFIXES:
            report.findings.append(
                PreflightFinding(
                    "warning",
                    "UNSUPPORTED_FILE",
                    f"{p.name} ({preview.suffix or 'no suffix'}) will be skipped — "
                    "supported types: csv, xlsx, xlsm, xls, pdf.",
                )
            )

    if not any(f.suffix in SUPPORTED_SUFFIXES for f in report.files):
        report.findings.append(
            PreflightFinding(
                "error",
                "NO_SUPPORTED_FILES",
                "No supported file types in the inbox — nothing for the parser "
                "to read. Drop csv/xlsx/pdf files in and re-run.",
            )
            )
        return report

    # ---- 3. Parse each supported file independently to catch duplicates
    # and supported-but-unparsed reference exports before the merged ingest.
    mile_sources: list[str] = []
    fuel_sources: list[str] = []
    for preview in report.files:
        if preview.suffix not in SUPPORTED_SUFFIXES:
            continue
        path = inbox / preview.name
        try:
            file_data = ingest_file(path)
        except Exception as e:
            report.findings.append(
                PreflightFinding(
                    "warning",
                    "FILE_PARSE_FAILED",
                    f"{preview.name} could not be parsed and will be skipped: {e}",
                )
            )
            continue
        preview.parsed_mile_rows = len(file_data.miles)
        preview.parsed_fuel_rows = len(file_data.fuel)
        preview.parsed_miles_total = round(sum(row.miles for row in file_data.miles), 2)
        preview.parsed_fuel_total = round(sum(row.gallons for row in file_data.fuel), 3)
        if file_data.miles:
            mile_sources.append(preview.name)
        if file_data.fuel:
            fuel_sources.append(preview.name)
        if not file_data.miles and not file_data.fuel:
            report.findings.append(
                PreflightFinding(
                    "warning",
                    "SUPPORTED_FILE_UNPARSED",
                    f"{preview.name} is a supported type but no mileage or fuel rows "
                    "were parsed. Treat it as reference unless the parser is updated.",
                )
            )

    if len(mile_sources) > 1:
        report.findings.append(
            PreflightFinding(
                "warning",
                "MULTIPLE_MILEAGE_SOURCES",
                f"Multiple files parsed mileage rows: {mile_sources}. Confirm these "
                "are separate sources, not duplicate summary/detail exports.",
            )
        )
        _add_duplicate_total_findings(report, kind="mileage")
    if len(fuel_sources) > 1:
        report.findings.append(
            PreflightFinding(
                "warning",
                "MULTIPLE_FUEL_SOURCES",
                f"Multiple files parsed fuel rows: {fuel_sources}. Confirm these are "
                "not duplicate summary/detail exports before filing.",
            )
        )
        _add_duplicate_total_findings(report, kind="fuel")

    # ---- 4. Parse and inspect the merged structure ----
    try:
        data = ingest_folder(inbox)
    except Exception as e:
        report.findings.append(
            PreflightFinding(
                "error",
                "INGEST_FAILED",
                f"Ingest crashed on the inbox files: {e}",
            )
        )
        return report

    report.trucks_in_miles = sorted({r.truck_id for r in data.miles})
    report.trucks_in_fuel = sorted({r.truck_id for r in data.fuel})
    report.mile_rows = len(data.miles)
    report.fuel_rows = len(data.fuel)
    report.total_miles = round(sum(row.miles for row in data.miles), 2)
    report.total_gallons = round(sum(row.gallons for row in data.fuel), 3)
    report.raw_mpg = round(report.total_miles / report.total_gallons, 4) if report.total_gallons else 0.0
    report.drivers = dict(data.truck_drivers)
    report.cards = dict(data.truck_cards)

    # ---- 5. Need BOTH miles and fuel data ----
    if not data.miles:
        report.findings.append(
            PreflightFinding(
                "error",
                "NO_MILES",
                "No mileage rows parsed. Verify the miles file has truck/state/miles columns.",
            )
        )
    if not data.fuel:
        report.findings.append(
            PreflightFinding(
                "error",
                "NO_FUEL",
                "No fuel rows parsed. Verify the fuel file has truck/state/gallons columns.",
            )
        )

    if data.miles and data.fuel:
        _add_raw_mpg_finding(report)

    # ---- 6. Truck IDs reconcile between miles and fuel ----
    miles_set = set(report.trucks_in_miles)
    fuel_set = set(report.trucks_in_fuel)
    only_miles = miles_set - fuel_set
    only_fuel = fuel_set - miles_set
    if only_miles:
        report.findings.append(
            PreflightFinding(
                "warning",
                "TRUCKS_ONLY_IN_MILES",
                f"These trucks have miles but no fuel purchases: {sorted(only_miles)}. "
                "If they ran this quarter, fuel data is missing.",
            )
        )
    if only_fuel:
        report.findings.append(
            PreflightFinding(
                "warning",
                "TRUCKS_ONLY_IN_FUEL",
                f"These trucks have fuel purchases but no miles: {sorted(only_fuel)}. "
                "Confirm they actually drove this quarter.",
            )
        )

    # ---- 7. "unknown" truck — parser couldn't identify the unit ----
    if "unknown" in miles_set or "unknown" in fuel_set:
        report.findings.append(
            PreflightFinding(
                "warning",
                "UNKNOWN_TRUCK",
                "Some rows had no resolvable truck_id and were bucketed as 'unknown'. "
                "Check the raw file's truck/unit/vehicle column.",
            )
        )

    # ---- 8. Sanity: very few rows for a full quarter ----
    if data.miles and len(data.miles) < 5:
        report.findings.append(
            PreflightFinding(
                "warning",
                "MILES_SUSPICIOUSLY_FEW",
                f"Only {len(data.miles)} mileage rows parsed. A typical quarter has "
                "dozens to hundreds. Either parsing missed rows or this is light activity.",
            )
        )
    if data.fuel and len(data.fuel) < 3:
        report.findings.append(
            PreflightFinding(
                "warning",
                "FUEL_SUSPICIOUSLY_FEW",
                f"Only {len(data.fuel)} fuel rows parsed. Check that the fuel file "
                "is the right one and the columns were detected.",
            )
        )

    return report


def _add_duplicate_total_findings(
    report: PreflightReport, *, kind: Literal["mileage", "fuel"]
) -> None:
    """Block likely summary/detail duplicates with identical parsed totals."""
    seen: dict[float, str] = {}
    for preview in report.files:
        if kind == "mileage":
            if not preview.parsed_mile_rows:
                continue
            total = preview.parsed_miles_total
            code = "DUPLICATE_MILEAGE_SOURCE"
            unit = "miles"
        else:
            if not preview.parsed_fuel_rows:
                continue
            total = preview.parsed_fuel_total
            code = "DUPLICATE_FUEL_SOURCE"
            unit = "gallons"
        if total <= 0:
            continue
        existing = seen.get(total)
        if existing is None:
            seen[total] = preview.name
            continue
        report.findings.append(
            PreflightFinding(
                "error",
                code,
                f"{existing} and {preview.name} both parsed {total:,.2f} {unit}. "
                "This looks like a duplicate summary/detail export. Remove one "
                "source or re-run with --force only after manual confirmation.",
            )
        )


def _add_raw_mpg_finding(report: PreflightReport) -> None:
    sanity = load_kb()["fleet_mpg_calculation"]["sanity_range"]
    mpg_lo = float(sanity["min_realistic_heavy_diesel"])
    mpg_hi = float(sanity["max_realistic_heavy_diesel"])
    if report.raw_mpg == 0:
        return

    # RAW_MPG_HIGH/LOW are *data-quality* signals, not parse failures: the
    # files were read fine, the ratio just suggests missing fuel (high) or
    # duplicate fuel / missing miles (low). The agent's domain knowledge
    # (Step 1) interprets these correctly and asks the customer for the
    # missing pieces, so they must NEVER hard-block the submission — that
    # would deny the agent the chance to help. Keep them as warnings so the
    # operator + agent can decide what to do with the data they have.
    if report.raw_mpg > mpg_hi:
        report.findings.append(
            PreflightFinding(
                "warning",
                "RAW_MPG_HIGH",
                f"Raw miles/gallons MPG is {report.raw_mpg:.2f} "
                f"({report.total_miles:,.0f} miles / {report.total_gallons:,.2f} gal), "
                f"above the expected heavy-diesel range up to {mpg_hi}. "
                "This usually means missing fuel files, date-range mismatch, or duplicate miles.",
            )
        )
    elif report.raw_mpg < mpg_lo:
        report.findings.append(
            PreflightFinding(
                "warning",
                "RAW_MPG_LOW",
                f"Raw miles/gallons MPG is {report.raw_mpg:.2f} "
                f"({report.total_miles:,.0f} miles / {report.total_gallons:,.2f} gal), "
                f"below the expected heavy-diesel range starting around {mpg_lo}. "
                "This usually means missing miles, duplicate fuel, or wrong units.",
            )
        )


def format_preflight(report: PreflightReport) -> str:
    """Human-readable rendering for terminal output."""
    if not report.findings:
        return "Preflight: clean."
    by_sev: dict[str, list[PreflightFinding]] = {"error": [], "warning": [], "info": []}
    for f in report.findings:
        by_sev[f.severity].append(f)
    parts: list[str] = []
    for sev in ("error", "warning", "info"):
        items = by_sev[sev]
        if not items:
            continue
        parts.append(f"\n{sev.upper()}S ({len(items)}):")
        for f in items:
            parts.append(f"  [{f.code}] {f.message}")
    return "\n".join(parts).strip()
