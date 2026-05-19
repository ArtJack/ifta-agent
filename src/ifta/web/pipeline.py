"""Drive the IFTA pipeline + AI agent review for an anonymous web submission.

Reads uploads from `<submissions_dir>/<sid>/inbox/<quarter>/`, writes outputs to
`<submissions_dir>/<sid>/outputs/<quarter>/`. Invokes the agent with explicit
inbox/output path overrides so the agent's tools find the submission's data
(instead of the conventional `inbox/<client>/<quarter>/` paths).

When the agent call fails (API outage, missing key, JSON parse failure), the
worker falls back to writing a deterministic findings note so the customer
still gets a packet — just without the AI narrative.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ifta.calc import compute_per_truck_lines, compute_return
from ifta.ingest import ingest_folder
from ifta.preflight import format_preflight, preflight_inputs
from ifta.rates import fetch_rates
from ifta.report import write_per_truck_filings, write_portal_csv
from ifta.validator import format_findings, validate
from ifta.web.models import Submission

log = logging.getLogger("ifta.web.pipeline")


class PipelineError(Exception):
    """The pipeline could not produce a packet for this submission."""


def process_submission(
    submissions_dir: Path, sub: Submission, *, fuel: str = "diesel"
) -> Path:
    """Run preflight → ingest → compute → agent review → write for one submission.

    Returns the output directory. Raises PipelineError on any failure that
    the customer needs to act on (bad files, missing data, preflight errors).
    Agent failures are non-fatal — the deterministic packet still ships.
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

    _write_review_note(
        out_dir / "review_note.md",
        sub=sub,
        inbox=inbox,
        out_dir=out_dir,
        findings=findings,
        ret=ret,
    )
    return out_dir


def _write_review_note(
    path: Path,
    *,
    sub: Submission,
    inbox: Path,
    out_dir: Path,
    findings: list,
    ret,
) -> None:
    """Try the AI agent first; fall back to a deterministic findings note."""
    if os.environ.get("ANTHROPIC_API_KEY") and not _agent_disabled():
        try:
            _write_agent_review(path, sub=sub, inbox=inbox, out_dir=out_dir)
            return
        except Exception as e:
            log.warning(
                "agent review failed for submission %s — writing deterministic "
                "note instead: %s",
                sub.id,
                e,
            )
    _write_findings_note(path, findings, ret)


def _agent_disabled() -> bool:
    """Honor an env-var kill switch (useful for tests + dev)."""
    return os.environ.get("IFTA_WEB_SKIP_AGENT", "").lower() in {"1", "true", "yes"}


def _write_agent_review(
    path: Path,
    *,
    sub: Submission,
    inbox: Path,
    out_dir: Path,
) -> None:
    """Run the agent against this submission's paths and write its narrative."""
    # Imported lazily so unrelated tests don't pay the agent's import cost.
    from ifta.agent import review as agent_review
    from ifta.agent import write_review_md

    model = os.environ.get("IFTA_WEB_AGENT_MODEL", "claude-sonnet-4-6")
    effort = os.environ.get("IFTA_WEB_AGENT_EFFORT", "medium")

    log.info("running agent review for submission %s (model=%s)", sub.id, model)
    note, metrics = agent_review(
        sub.quarter,
        model=model,
        effort=effort,
        inbox_dir=inbox,
        output_dir=out_dir,
        client_name=sub.company or "Anonymous web submission",
    )
    written = write_review_md(note, path, metrics=metrics)
    log.info(
        "agent review written to %s (%.1fs · $%.4f)",
        written,
        metrics.wall_time_seconds,
        metrics.estimated_cost_usd,
    )


def _write_findings_note(path: Path, findings: list, ret) -> None:
    """Deterministic fallback note when the agent is unavailable."""
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
        "_AI review was skipped for this submission — packet contains the "
        "deterministic pipeline output only._"
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
