"""Drive the deterministic IFTA pipeline for an anonymous web submission.

Reads uploads from `<submissions_dir>/<sid>/inbox/<quarter>/`, writes outputs to
`<submissions_dir>/<sid>/outputs/<quarter>/`. Skips the AI review for now —
the agent's tools resolve via (PROJECT_ROOT, quarter, client) and anonymous
web submissions don't fit that contract cleanly. Wiring the agent in is
deferred to a later phase.
"""

from __future__ import annotations

from pathlib import Path

from ifta.calc import compute_per_truck_lines, compute_return
from ifta.ingest import ingest_folder
from ifta.preflight import format_preflight, preflight_inputs
from ifta.rates import fetch_rates
from ifta.report import write_per_truck_filings, write_portal_csv
from ifta.validator import format_findings, validate
from ifta.web.models import Submission


class PipelineError(Exception):
    """The pipeline could not produce a packet for this submission."""


def process_submission(
    submissions_dir: Path, sub: Submission, *, fuel: str = "diesel"
) -> Path:
    """Run preflight → ingest → compute → write for one submission.

    Returns the output directory. Raises PipelineError on any failure that
    the customer needs to act on (bad files, missing data, preflight errors).
    """
    inbox = submissions_dir / sub.id / "inbox" / sub.quarter
    out_dir = submissions_dir / sub.id / "outputs" / sub.quarter

    if not inbox.exists():
        raise PipelineError(f"inbox not found: {inbox}")

    out_dir.mkdir(parents=True, exist_ok=True)

    report = preflight_inputs(inbox)
    if report.has_errors:
        raise PipelineError(
            "Preflight found ERROR-level issues in your uploaded files:\n"
            + format_preflight(report)
        )

    data = ingest_folder(inbox)
    if not data.miles and not data.fuel:
        raise PipelineError(
            "No usable data parsed from the uploaded files. "
            "Expected mileage by truck/state and fuel by truck/state."
        )

    rates_table = fetch_rates(sub.quarter, fuel=fuel)
    ret = compute_return(data, rates_table)
    findings = validate(data, ret)

    write_portal_csv(ret, out_dir / "ifta_portal.csv", portal="generic")

    per_truck_lines = compute_per_truck_lines(data, ret, rates_table)
    write_per_truck_filings(
        per_truck_lines,
        fleet_mpg=ret.fleet_mpg,
        quarter=ret.quarter,
        client_name=sub.company or "Web Submission",
        fuel=ret.fuel,
        out_dir=out_dir / "trucks",
        data=data,
    )

    _write_findings_note(out_dir / "review_note.md", findings, ret)
    return out_dir


def _write_findings_note(path: Path, findings: list, ret) -> None:
    """Placeholder review note — concrete findings only, no agent narrative yet."""
    lines = [
        f"# IFTA Review — {ret.quarter}",
        "",
        f"- Fleet miles: **{ret.fleet_miles:,.0f}**",
        f"- Fleet gallons: **{ret.fleet_gallons:,.2f}**",
        f"- Fleet MPG: **{ret.fleet_mpg:.4f}**",
        f"- Total tax due: **${ret.total_tax_due:,.2f}**",
        "",
    ]
    if findings:
        lines.append("## Validator findings\n")
        lines.append("```")
        lines.append(format_findings(findings))
        lines.append("```")
        lines.append("")
    if ret.rate_warning:
        lines.append("> **Rate warning:** " + ret.rate_warning + "\n")
    lines.append(
        "_AI review is not enabled for web submissions yet — this packet "
        "contains the deterministic pipeline output only._"
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
