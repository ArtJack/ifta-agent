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

from ifta.ingest import ingest_folder

SUPPORTED_SUFFIXES = {".csv", ".xlsx", ".xlsm", ".xls", ".pdf"}
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
                }
                for f in self.files
            ],
            "trucks_in_miles": self.trucks_in_miles,
            "trucks_in_fuel": self.trucks_in_fuel,
            "mile_rows": self.mile_rows,
            "fuel_rows": self.fuel_rows,
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
        if suffix in (".xlsx", ".xlsm", ".xls"):
            import openpyxl  # local import: openpyxl is heavy

            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            note = f"sheets: {', '.join(wb.sheetnames)}"
            wb.close()
        elif suffix == ".csv":
            with path.open(encoding="utf-8", errors="replace") as fh:
                header = fh.readline().strip()
            note = f"header: {header[:120]}"
        elif suffix == ".pdf":
            note = "pdf"
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

    # ---- 2. Each file is a supported format ----
    for p in files:
        preview = _peek_file(p)
        report.files.append(preview)
        if preview.suffix not in SUPPORTED_SUFFIXES:
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

    # ---- 3. Parse and inspect the structure ----
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
    report.drivers = dict(data.truck_drivers)
    report.cards = dict(data.truck_cards)

    # ---- 4. Need BOTH miles and fuel data ----
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

    # ---- 5. Truck IDs reconcile between miles and fuel ----
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

    # ---- 6. "unknown" truck — parser couldn't identify the unit ----
    if "unknown" in miles_set or "unknown" in fuel_set:
        report.findings.append(
            PreflightFinding(
                "warning",
                "UNKNOWN_TRUCK",
                "Some rows had no resolvable truck_id and were bucketed as 'unknown'. "
                "Check the raw file's truck/unit/vehicle column.",
            )
        )

    # ---- 7. Sanity: very few rows for a full quarter ----
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
