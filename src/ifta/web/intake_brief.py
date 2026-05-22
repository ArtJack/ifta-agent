"""Generate a deterministic intake brief for a web submission.

The brief is a short markdown file summarizing what was uploaded and who
submitted it. The summary is a 2-3 sentence text version used in the
Telegram approval card.

No AI, no network calls -- just filesystem inspection and string formatting.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ifta.preflight import preflight_inputs
from ifta.web.models import Submission

log = logging.getLogger("ifta.web.intake_brief")


def generate_intake_brief(
    submissions_dir: Path,
    sub: Submission,
    *,
    inbox_path: Path | None = None,
) -> tuple[str, str]:
    """Return (brief_path, summary_text) for a submission.

    ``brief_path`` is relative to ``submissions_dir`` so it stays portable
    across mounts.  ``summary_text`` is a compact plain-text string for the
    Telegram approval card.
    """
    inbox = inbox_path or (submissions_dir / sub.id / "inbox" / sub.quarter)
    brief_dir = submissions_dir / sub.id
    brief_file = brief_dir / "intake_brief.md"
    brief_dir.mkdir(parents=True, exist_ok=True)

    # --- gather file info ---------------------------------------------------
    file_lines: list[str] = []
    if inbox.exists():
        for p in sorted(inbox.iterdir()):
            if p.is_file() and not p.name.startswith("."):
                size_kb = p.stat().st_size / 1024
                file_lines.append(f"- {p.name} ({size_kb:.0f} KB)")

    # --- preflight ----------------------------------------------------------
    preflight_lines: list[str] = []
    if inbox.exists():
        try:
            report = preflight_inputs(inbox)
            preflight_lines.append(f"- Mileage rows parsed: {report.mile_rows}")
            preflight_lines.append(f"- Fuel rows parsed: {report.fuel_rows}")
            if report.trucks_in_miles or report.trucks_in_fuel:
                all_trucks = sorted(
                    set(report.trucks_in_miles) | set(report.trucks_in_fuel)
                )
                preflight_lines.append(f"- Trucks detected: {', '.join(all_trucks)}")
            if report.findings:
                for f in report.findings:
                    preflight_lines.append(f"- [{f.severity.upper()}] {f.code}: {f.message}")
            else:
                preflight_lines.append("- Preflight clean")
        except Exception as e:
            preflight_lines.append(f"- Preflight error: {e}")

    # --- build markdown brief -----------------------------------------------
    md_lines = [
        f"# Intake Brief -- {sub.quarter}",
        "",
        "## Customer",
        f"- Name: {sub.name or '(not provided)'}",
        f"- Email: {sub.email}",
        f"- Company: {sub.company or '(not provided)'}",
        f"- Base state: {sub.base_state or '(not provided)'}",
        f"- Fleet size: {sub.fleet_size or '(not provided)'}",
        "",
    ]
    if sub.notes:
        md_lines += [
            "## Notes",
            sub.notes,
            "",
        ]
    md_lines += [
        "## Uploaded files",
        *(file_lines or ["- (no files found)"]),
        "",
        "## Preflight",
        *(preflight_lines or ["- (not run)"]),
        "",
        "## Submission",
        f"- ID: {sub.id}",
        f"- Quarter: {sub.quarter}",
        f"- Created: {sub.created_at.isoformat() if sub.created_at else '?'}",
    ]

    brief_text = "\n".join(md_lines) + "\n"
    brief_file.write_text(brief_text, encoding="utf-8")
    brief_path = str(brief_file.relative_to(submissions_dir))

    # --- build summary text -------------------------------------------------
    company_part = f" ({sub.company})" if sub.company else ""
    name_part = sub.name or sub.email
    file_count = len(file_lines)
    fleet_part = f", fleet size {sub.fleet_size}" if sub.fleet_size else ""
    base_part = f" based in {sub.base_state}" if sub.base_state else ""

    preflight_status = "clean"
    if preflight_lines:
        error_count = sum(1 for line in preflight_lines if "[ERROR]" in line)
        warn_count = sum(1 for line in preflight_lines if "[WARNING]" in line)
        if error_count:
            preflight_status = f"{error_count} error(s)"
        elif warn_count:
            preflight_status = f"{warn_count} warning(s)"

    summary = (
        f"{name_part}{company_part} submitted {file_count} file(s) for "
        f"{sub.quarter}{base_part}{fleet_part}. "
        f"Preflight: {preflight_status}."
    )

    return brief_path, summary
