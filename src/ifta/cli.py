"""CLI entrypoint: `python -m ifta run --quarter Q1-2026`."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from ifta.calc import compute_per_truck_lines, compute_return
from ifta.client import (
    load_client_context,
    load_registry,
    quarter_key,
    resolve_inbox,
    resolve_output_dir,
)
from ifta.ingest import ingest_folder
from ifta.rates import fetch_rates
from ifta.report import write_cleaned_csvs, write_per_truck_filings, write_portal_csv
from ifta.validator import format_findings, validate

console = Console()
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _display_path(p: Path) -> str:
    """Show paths relative to PROJECT_ROOT when they're inside it, otherwise
    fall back to the absolute path. Stops `relative_to` from raising when a
    user passes --out to a location outside the project tree."""
    try:
        return str(p.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(p)


def _parse_quarter(quarter: str) -> str:
    """Quarter format validation at the CLI boundary — surface validation
    errors as ClickExceptions (clean one-liners) instead of letting
    ValueError bubble up as a stack trace."""
    try:
        return quarter_key(quarter)
    except ValueError as e:
        raise click.ClickException(str(e)) from e


@click.group()
def main() -> None:
    """IFTA quarterly filing pipeline."""


@main.command()
@click.option("--quarter", required=True, help="e.g. Q1-2026 or 1Q2026")
@click.option("--fuel", default="diesel", show_default=True)
@click.option("--client", default=None, help="Client id/name, e.g. dm_express or test_logistics")
@click.option(
    "--portal",
    default=None,
    type=click.Choice(["generic", "cdtfa", "ky"]),
    help="Portal flavor for worksheet labels. Defaults to client metadata.",
)
@click.option("--inbox", default=None, type=click.Path(path_type=Path))
@click.option("--out", "out_dir", default=None, type=click.Path(path_type=Path))
@click.option("--refresh-rates", is_flag=True, help="re-fetch from iftach.org even if cached")
def run(
    quarter: str,
    fuel: str,
    client: str | None,
    portal: str | None,
    inbox: Path | None,
    out_dir: Path | None,
    refresh_rates: bool,
) -> None:
    """Process raw files in inbox/<quarter>/ and emit outputs/<quarter>/."""
    qkey = _parse_quarter(quarter)
    inbox = (inbox or resolve_inbox(PROJECT_ROOT, qkey, client)).resolve()
    out_dir = (out_dir or resolve_output_dir(PROJECT_ROOT, qkey, client)).resolve()
    client_context = load_client_context(PROJECT_ROOT, qkey, client=client, inbox=inbox)
    portal_name = portal or client_context.portal or "generic"

    if not inbox.exists():
        raise click.ClickException(f"inbox not found: {inbox}")

    console.rule(f"[bold]IFTA {qkey}")
    console.print(f"  client: {client_context.client_name} ({client_context.client_id})")
    console.print(f"  portal: {portal_name}")
    console.print(f"  inbox:  {inbox}")
    console.print(f"  out:    {out_dir}")

    console.print("\n[bold]1. Ingesting raw files…")
    data = ingest_folder(inbox)
    console.print(f"  trucks: {data.trucks}")
    console.print(
        f"  states: {len(data.states)}  mile-rows: {len(data.miles)}  fuel-rows: {len(data.fuel)}"
    )
    if not data.miles and not data.fuel:
        raise click.ClickException("no usable data parsed from inbox files")

    console.print("\n[bold]2. Fetching IFTA rates…")
    rates = fetch_rates(qkey, fuel=fuel, force=refresh_rates)
    console.print(f"  loaded {len(rates.rates)} jurisdictions ({rates.fuel})")
    if rates.warning:
        console.print(f"  [bold yellow]WARNING:[/] {rates.warning}")

    console.print("\n[bold]3. Computing return…")
    ret = compute_return(data, rates)
    console.print(f"  fleet miles:   {ret.fleet_miles:,.0f}")
    console.print(f"  fleet gallons: {ret.fleet_gallons:,.2f}")
    console.print(f"  fleet MPG:     {ret.fleet_mpg:.4f}")
    console.print(f"  TOTAL TAX DUE: ${ret.total_tax_due:,.2f}")

    console.print("\n[bold]4. Validating…")
    findings = validate(data, ret)
    if findings:
        console.print(format_findings(findings))
    else:
        console.print("  no issues found")

    console.print("\n[bold]5. Writing outputs…")
    portal_csv = write_portal_csv(ret, out_dir / "ifta_portal.csv", portal=portal_name)
    console.print(f"  ✓ {_display_path(portal_csv)}")
    per_truck_lines = compute_per_truck_lines(data, ret, rates)
    truck_paths = write_per_truck_filings(
        per_truck_lines,
        fleet_mpg=ret.fleet_mpg,
        quarter=ret.quarter,
        client_name=client_context.client_name,
        fuel=ret.fuel,
        out_dir=out_dir / "trucks",
        data=data,
    )
    for tp in truck_paths:
        console.print(f"  ✓ {_display_path(tp)}")
    console.print(
        "[dim]  (Note: ifta run skips the AI review — use 'ifta deliver' for "
        "review_note.md too.)[/]"
    )

    _print_summary(ret)


def _print_summary(ret) -> None:
    table = Table(title=f"Per-state summary — {ret.quarter}")
    table.add_column("State")
    table.add_column("Miles", justify="right")
    table.add_column("Paid Gal", justify="right")
    table.add_column("Taxable Gal", justify="right")
    table.add_column("Net Gal", justify="right")
    table.add_column("Rate", justify="right")
    table.add_column("Tax Due", justify="right")
    for line in ret.lines:
        style = "red" if line.is_credit else None
        table.add_row(
            line.state,
            f"{line.miles:,.0f}",
            f"{line.tax_paid_gal:,.2f}",
            f"{line.taxable_gal:,.2f}",
            f"{line.net_taxable_gal:,.2f}",
            f"{line.rate:.4f}",
            f"${line.tax_due:,.2f}",
            style=style,
        )
    console.print(table)


@main.command()
@click.option("--quarter", required=True)
@click.option("--fuel", default="diesel")
@click.option("--force", is_flag=True)
def rates(quarter: str, fuel: str, force: bool) -> None:
    """Just fetch & cache the IFTA tax rates for a quarter."""
    t = fetch_rates(quarter, fuel=fuel, force=force)
    console.print(f"[bold]{t.quarter}[/] ({t.fuel}) — {len(t.rates)} jurisdictions")
    for s in sorted(t.rates):
        console.print(f"  {s}: ${t.rates[s]:.4f}")


def _load_quarter(qkey: str, fuel: str, force: bool, client: str | None = None):
    """Re-run ingest + compute for a quarter; used by review/ask."""
    inbox = resolve_inbox(PROJECT_ROOT, qkey, client)
    if not inbox.exists():
        raise click.ClickException(f"inbox not found: {inbox}")
    data = ingest_folder(inbox)
    rates = fetch_rates(qkey, fuel=fuel, force=force)
    ret = compute_return(data, rates)
    findings = validate(data, ret)
    return data, ret, findings


MODEL_CHOICES = ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"]
EFFORT_CHOICES = ["low", "medium", "high", "xhigh", "max"]


@main.command()
@click.option("--quarter", required=True, help="e.g. Q4-2025")
@click.option("--client", default=None, help="Client id/name, e.g. dm_express or test_logistics")
@click.option(
    "--model",
    default="claude-sonnet-4-6",
    type=click.Choice(MODEL_CHOICES),
    show_default=True,
)
@click.option(
    "--max-tokens",
    default=4096,
    show_default=True,
    help="Output token ceiling per call.",
)
@click.option(
    "--effort",
    default="medium",
    type=click.Choice(EFFORT_CHOICES),
    show_default=True,
    help="Thinking depth — higher = more thorough, more expensive.",
)
def review(
    quarter: str,
    client: str | None,
    model: str,
    max_tokens: int,
    effort: str,
) -> None:
    """Pre-filing review: agent verifies the return before you upload to the gov portal."""
    from ifta.agent import (
        format_review_item,
        write_review_md,
    )
    from ifta.agent import (
        review as agent_review,
    )

    qkey = _parse_quarter(quarter)
    out_dir = resolve_output_dir(PROJECT_ROOT, qkey, client)
    client_context = load_client_context(PROJECT_ROOT, qkey, client=client)

    console.print(
        f"[bold]Reviewing {qkey} for {client_context.client_name} with {model} "
        f"(max_tokens={max_tokens}, effort={effort})…"
    )
    note, agent_metrics = agent_review(
        qkey, client=client, model=model, max_tokens=max_tokens, effort=effort
    )
    path = write_review_md(note, out_dir / "review_note.md", metrics=agent_metrics)
    console.print(f"  ✓ {_display_path(path)}")
    console.print(
        f"  [dim]Agent run: {agent_metrics.wall_time_seconds:.1f}s · "
        f"{agent_metrics.output_tokens:,} output tokens · "
        f"${agent_metrics.estimated_cost_usd:.4f}[/]"
    )
    console.rule("Review Note")
    if note.filing_status:
        console.print(f"[bold]Filing status:[/] {note.filing_status}")
    console.print(f"[bold]Summary:[/] {note.summary}")
    for section, items in [
        ("Issues", note.issues),
        ("Filing reminders", note.filing_reminders),
        ("Next steps", note.next_steps),
    ]:
        if items:
            console.print(f"\n[bold]{section}:[/]")
            for x in items:
                console.print(f"  • {format_review_item(x, checkbox=section == 'Next steps')}")


@main.command()
@click.option("--quarter", default=None, help="Focus on a specific quarter (optional)")
@click.option("--client", default=None, help="Client id/name, e.g. dm_express or test_logistics")
@click.option(
    "--model",
    default="claude-sonnet-4-6",
    type=click.Choice(MODEL_CHOICES),
    show_default=True,
)
@click.option("--max-tokens", default=2048, show_default=True)
@click.option("--effort", default="medium", type=click.Choice(EFFORT_CHOICES), show_default=True)
@click.argument("question")
def ask(
    quarter: str | None,
    client: str | None,
    model: str,
    max_tokens: int,
    effort: str,
    question: str,
) -> None:
    """One-shot Q&A. The agent uses tools to ground answers in real data."""
    from ifta.agent import ask as agent_ask

    qkey = _parse_quarter(quarter) if quarter else None
    console.print(f"[bold]Q:[/] {question}")
    answer = agent_ask(
        question,
        quarter=qkey,
        client=client,
        model=model,
        max_tokens=max_tokens,
        effort=effort,
    )
    console.print(f"\n[bold]A:[/] {answer}")


@main.command()
@click.option(
    "--model",
    default="claude-sonnet-4-6",
    type=click.Choice(MODEL_CHOICES),
    show_default=True,
)
@click.option("--max-tokens", default=4096, show_default=True)
@click.option("--effort", default="medium", type=click.Choice(EFFORT_CHOICES), show_default=True)
def chat(model: str, max_tokens: int, effort: str) -> None:
    """Interactive multi-turn chat with the IFTA Agent."""
    from ifta.agent import chat_loop

    chat_loop(model=model, max_tokens=max_tokens, effort=effort)


@main.command()
@click.option("--quarter", required=True, help="e.g. Q2-2026")
@click.option("--fuel", default="diesel", show_default=True)
@click.option("--client", default=None, help="Client id/name, e.g. dm_express or test_logistics")
@click.option(
    "--portal",
    default=None,
    type=click.Choice(["generic", "cdtfa", "ky"]),
    help="Portal flavor for worksheet labels. Defaults to client metadata.",
)
@click.option(
    "--model",
    default="claude-sonnet-4-6",
    type=click.Choice(MODEL_CHOICES),
    show_default=True,
    help="Agent model — Sonnet by default; use Opus for high-risk reviews.",
)
@click.option("--max-tokens", default=4096, show_default=True)
@click.option("--effort", default="medium", type=click.Choice(EFFORT_CHOICES), show_default=True)
@click.option("--skip-agent", is_flag=True, help="Skip the AI review step.")
@click.option("--no-open", is_flag=True, help="Don't open the output folder in Finder.")
@click.option(
    "--diagnostics",
    is_flag=True,
    help="Also write cleaned_miles.csv + cleaned_fuel.csv for debugging.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Skip preflight blocking — run compute even if preflight reports ERRORs.",
)
def deliver(
    quarter: str,
    fuel: str,
    client: str | None,
    portal: str | None,
    model: str,
    max_tokens: int,
    effort: str,
    skip_agent: bool,
    no_open: bool,
    diagnostics: bool,
    force: bool,
) -> None:
    """End-to-end: compute → validate → AI review → open the result folder.

    Output per customer per quarter (2 + N files for N trucks) —
        outputs/<quarter>/ifta_portal.csv      (upload to the gov portal)
        outputs/<quarter>/review_note.md       (agent report for the customer)
        outputs/<quarter>/trucks/truck_<id>.xlsx   (one per truck, Menshikov-CDTFA style;
                                                    customer forwards to each owner-operator)

    Pass --diagnostics for additional cleaned_miles.csv / cleaned_fuel.csv.
    """
    import subprocess

    qkey = _parse_quarter(quarter)
    inbox = resolve_inbox(PROJECT_ROOT, qkey, client).resolve()
    out_dir = resolve_output_dir(PROJECT_ROOT, qkey, client).resolve()
    if not inbox.exists():
        raise click.ClickException(f"inbox not found: {inbox}")
    client_context = load_client_context(PROJECT_ROOT, qkey, client=client, inbox=inbox)
    portal_name = portal or client_context.portal or "generic"

    from ifta.preflight import format_preflight, preflight_inputs

    console.rule(f"[bold cyan]IFTA Deliver — {qkey}")
    console.print(f"Client: {client_context.client_name} ({client_context.client_id})")
    console.print(f"Portal worksheet flavor: {portal_name}")

    # --- 0. Preflight (structural check on raw inputs) ---
    console.print("\n[bold]Step 0/4 — Preflight raw inputs…[/]")
    report = preflight_inputs(inbox)
    for f in report.files:
        console.print(f"  • {f.name}  [dim]({f.note})[/]")
    if report.findings:
        console.print(format_preflight(report))
    else:
        console.print("  [green]✓ Preflight clean[/]")
    if report.has_errors and not force:
        raise click.ClickException(
            "Preflight found ERROR-level issues. Fix the inbox or re-run with --force to ignore."
        )

    # --- 1. Pipeline ---
    console.print("\n[bold]Step 1/4 — Computing return from raw files…[/]")
    data = ingest_folder(inbox)
    rates_table = fetch_rates(qkey, fuel=fuel)
    ret = compute_return(data, rates_table)
    findings = validate(data, ret)

    console.print(
        f"  Trucks: {', '.join(data.trucks)}   States: {len(data.states)}   "
        f"Fleet MPG: {ret.fleet_mpg:.2f}"
    )
    console.print(
        f"  Fleet miles: {ret.fleet_miles:,.0f}   Fleet gallons: {ret.fleet_gallons:,.0f}"
    )
    console.print(f"  [bold]Total tax due: ${ret.total_tax_due:,.2f}[/]")
    if ret.rate_warning:
        console.print(f"  [bold yellow]WARNING:[/] {ret.rate_warning}")

    # --- 2. Always write the portal CSV; the owner-review xlsx is written
    # AFTER the agent runs so it can embed the agent's findings + metrics.
    portal_csv = write_portal_csv(ret, out_dir / "ifta_portal.csv", portal=portal_name)
    diagnostic_paths: list[Path] = []
    if diagnostics:
        miles_csv, fuel_csv = write_cleaned_csvs(data, out_dir)
        diagnostic_paths = [miles_csv, fuel_csv]

    console.print("\n[bold]Step 2/4 — Files written so far[/]")
    console.print(f"  ✓ {_display_path(portal_csv)}")
    for p in diagnostic_paths:
        console.print(f"  ✓ {_display_path(p)} [dim](diagnostic)[/]")

    if findings:
        console.print("\n[bold yellow]Validator findings:[/]")
        console.print(format_findings(findings))

    # --- 3. Agent review (1 report per customer per quarter) ---
    note_path: Path | None = None
    if not skip_agent:
        console.print(f"\n[bold]Step 3/4 — Agent review ({model}, effort={effort})…[/]")
        try:
            from ifta.agent import (
                format_review_item,
                write_review_md,
            )
            from ifta.agent import (
                review as agent_review,
            )

            note, agent_metrics = agent_review(
                qkey,
                client=client,
                model=model,
                max_tokens=max_tokens,
                effort=effort,
            )
            note_path = write_review_md(note, out_dir / "review_note.md", metrics=agent_metrics)
            console.print(f"  ✓ {_display_path(note_path)}")
            console.print(
                f"  [dim]Agent: {agent_metrics.wall_time_seconds:.1f}s · "
                f"{agent_metrics.input_tokens + agent_metrics.cache_read_tokens + agent_metrics.cache_creation_tokens:,} in / "
                f"{agent_metrics.output_tokens:,} out tokens · "
                f"${agent_metrics.estimated_cost_usd:.4f}[/]"
            )

            console.rule("[bold green]Agent Review")
            if note.filing_status:
                console.print(f"[bold]Filing status:[/] {note.filing_status}")
            console.print(f"[bold]Summary:[/] {note.summary}")
            for section, items in [
                ("Issues", note.issues),
                ("Filing reminders", note.filing_reminders),
                ("Next steps", note.next_steps),
            ]:
                if items:
                    console.print(f"\n[bold]{section}:[/]")
                    for x in items:
                        console.print(
                            f"  • {format_review_item(x, checkbox=section == 'Next steps')}"
                        )
        except Exception as e:
            console.print(
                f"[red]Agent step failed:[/] {e}\n"
                "[dim]Portal CSV is still ready. Re-run with --skip-agent "
                "to suppress the AI review.[/]"
            )

    # --- 4. Per-truck Excel files (one per owner-operator) ---
    per_truck_lines = compute_per_truck_lines(data, ret, rates_table)
    truck_paths = write_per_truck_filings(
        per_truck_lines,
        fleet_mpg=ret.fleet_mpg,
        quarter=ret.quarter,
        client_name=client_context.client_name,
        fuel=ret.fuel,
        out_dir=out_dir / "trucks",
        data=data,
    )
    console.print(f"\n[bold]Step 4/4 — Per-truck files ({len(truck_paths)} trucks)[/]")
    for tp in truck_paths:
        console.print(f"  ✓ {_display_path(tp)}")

    # --- 5. Final summary box ---
    console.rule("[bold green]✓ DONE")
    if ret.rate_fallback_used:
        console.print(
            "\n[bold yellow]Do not upload yet:[/] current-quarter rates were not confirmed. "
            f"Use this worksheet only for review:\n  {portal_csv}\n"
        )
    else:
        console.print(f"\n[bold]Upload this file to the gov portal:[/]\n  {portal_csv}\n")
    if note_path:
        console.print(f"[bold]Agent report:[/]\n  {note_path}\n")
    if truck_paths:
        console.print(
            f"[bold]Per-truck files ({len(truck_paths)}) — forward to each owner-operator:[/]"
        )
        for tp in truck_paths:
            console.print(f"  {tp}")
        console.print()
    console.print(f"[dim]All files in: {_display_path(out_dir)}/[/]")

    # --- 6. Open in Finder (macOS) ---
    if not no_open:
        import contextlib

        with contextlib.suppress(FileNotFoundError):
            subprocess.run(["open", str(out_dir)], check=False)


@main.command(name="intake")
@click.option("--quarter", required=True, help="e.g. Q1-2026")
@click.option("--client", default=None, help="Client id/name, e.g. dm_express")
@click.option("--inbox", default=None, type=click.Path(path_type=Path))
@click.option("--out", "out_dir", default=None, type=click.Path(path_type=Path))
@click.option(
    "--receipt-candidates",
    default=None,
    type=click.Path(path_type=Path),
    help="JSON list of OCR/vision/manual receipt candidates. Defaults to inbox/receipt_candidates.json.",
)
def intake(
    quarter: str,
    client: str | None,
    inbox: Path | None,
    out_dir: Path | None,
    receipt_candidates: Path | None,
) -> None:
    """Inspect a messy customer upload before computing the IFTA return."""
    from ifta.intake.report import build_intake_payload, write_intake_outputs

    qkey = _parse_quarter(quarter)
    inbox = (inbox or resolve_inbox(PROJECT_ROOT, qkey, client)).resolve()
    out_dir = (out_dir or resolve_output_dir(PROJECT_ROOT, qkey, client)).resolve()
    receipt_candidates = receipt_candidates or inbox / "receipt_candidates.json"
    if not receipt_candidates.exists():
        receipt_candidates = None

    payload, proposals = build_intake_payload(
        inbox,
        quarter=qkey,
        receipt_candidates_path=receipt_candidates,
    )
    paths = write_intake_outputs(payload, proposals, out_dir)

    console.rule(f"[bold]IFTA Intake — {qkey}")
    console.print(f"  inbox:  {inbox}")
    console.print(f"  out:    {out_dir}")
    console.print(f"  status: [bold]{payload['status']}[/]")
    console.print(f"  files:  {len(payload['preflight']['files'])}")
    console.print(f"  findings: {len(payload['preflight']['findings'])}")
    console.print(f"  receipt candidates: {len(payload['receipt_reviews'])}")
    console.print(f"  proposed additions: {len(proposals)}")
    for label, path in paths.items():
        console.print(f"  ✓ {label}: {_display_path(path)}")


@main.command(name="intake-apply")
@click.option("--quarter", required=True, help="e.g. Q1-2026")
@click.option("--client", default=None, help="Client id/name, e.g. dm_express")
@click.option("--inbox", default=None, type=click.Path(path_type=Path))
@click.option(
    "--proposed",
    default=None,
    type=click.Path(path_type=Path, exists=True),
    help="proposed_fuel_additions.csv with approved=yes rows. Defaults to outputs/<quarter>/proposed_fuel_additions.csv.",
)
@click.option(
    "--out",
    "out_path",
    default=None,
    type=click.Path(path_type=Path),
    help="Derived fuel CSV path. Defaults to inbox/<quarter>/derived_fuel_from_receipts.csv.",
)
def intake_apply(
    quarter: str,
    client: str | None,
    inbox: Path | None,
    proposed: Path | None,
    out_path: Path | None,
) -> None:
    """Apply approved receipt-backed fuel additions into a derived CSV."""
    from ifta.intake.report import apply_approved_proposals_csv

    qkey = _parse_quarter(quarter)
    inbox = (inbox or resolve_inbox(PROJECT_ROOT, qkey, client)).resolve()
    proposed = (
        proposed or resolve_output_dir(PROJECT_ROOT, qkey, client) / "proposed_fuel_additions.csv"
    )
    out_path = out_path or inbox / "derived_fuel_from_receipts.csv"
    if not proposed.exists():
        raise click.ClickException(f"proposed additions CSV not found: {proposed}")

    count = apply_approved_proposals_csv(proposed, out_path)
    console.rule(f"[bold]IFTA Intake Apply — {qkey}")
    console.print(f"  proposed: {proposed}")
    console.print(f"  output:   {out_path}")
    console.print(f"  approved rows written: {count}")
    if count == 0:
        console.print("[yellow]No approved=yes rows were found.[/]")


@main.command(name="web")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True, type=int)
@click.option("--reload", is_flag=True, help="Reload on code changes (dev only).")
def web(host: str, port: int, reload: bool) -> None:
    """Run the FastAPI web intake server (customer upload endpoint)."""
    import uvicorn
    from dotenv import load_dotenv

    # override=True so .env always wins over the shell — some launchers
    # (Claude Desktop, certain IDEs) export ANTHROPIC_API_KEY="" as a
    # sandbox, and load_dotenv's default behavior of "don't clobber existing
    # vars" would then silently leave the agent disabled.
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    console.print(f"[bold]Starting IFTA web intake[/] on http://{host}:{port}")
    uvicorn.run(
        "ifta.web.app:create_app",
        host=host,
        port=port,
        reload=reload,
        factory=True,
    )


@main.command(name="worker")
@click.option(
    "--poll-interval",
    default=5.0,
    show_default=True,
    type=float,
    help="Seconds to sleep when the queue is empty.",
)
@click.option("--once", is_flag=True, help="Process at most one job and exit (for ops/cron).")
def worker(poll_interval: float, once: bool) -> None:
    """Run the web-intake polling worker.

    Drains submissions from the SQLite jobs table; runs the deterministic IFTA
    pipeline; on success/failure emails the customer (if RESEND_API_KEY is set).
    """
    import logging
    from pathlib import Path as _Path

    from dotenv import load_dotenv

    from ifta.backup import archive_inputs, default_archive_root
    from ifta.notify import AdminNotifier, format_event, load_admin_notifier_config
    from ifta.web import db as _db
    from ifta.web import worker as worker_module
    from ifta.web.app import get_db_path, get_submissions_dir
    from ifta.web.email import EmailClient, load_email_config_from_env
    from ifta.web.models import Submission

    # override=True so .env always wins over the shell — some launchers
    # (Claude Desktop, certain IDEs) export ANTHROPIC_API_KEY="" as a
    # sandbox, and load_dotenv's default behavior of "don't clobber existing
    # vars" would then silently leave the agent disabled.
    load_dotenv(PROJECT_ROOT / ".env", override=True)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    db_path = get_db_path()
    submissions_dir = get_submissions_dir()
    # Schema is created by create_app() on the web side, but the worker may
    # start first (launchd RunAtLoad on both agents, no ordering guarantee).
    # init_db is idempotent — safe to call here too.
    _db.init_db(db_path)
    email_client = EmailClient(load_email_config_from_env())
    notifier = AdminNotifier(load_admin_notifier_config())

    def on_success(sub: Submission, out_dir: _Path) -> None:
        if email_client.send_packet(sub, out_dir):
            _db.mark_packet_sent(db_path, sub.id)
        # Reserve a dated copy of the raw inputs (best-effort; never fatal).
        backed_up: list[_Path] = []
        try:
            backed_up = archive_inputs(
                submissions_dir / sub.id / "inbox" / sub.quarter,
                archive_root=default_archive_root(submissions_dir),
                quarter=sub.quarter,
                company=sub.company,
                submission_id=sub.id,
            )
        except Exception:
            logging.getLogger("ifta.backup").exception(
                "input archival raised for %s", sub.id
            )
        try:
            notifier.send(
                format_event(
                    headline="✅ IFTA packet delivered",
                    source="web intake",
                    customer=sub.email,
                    quarter=sub.quarter,
                    extras={
                        "Company": sub.company or "—",
                        "Submission": sub.id,
                        "Backup": f"{len(backed_up)} file(s) archived",
                    },
                    review_note_path=out_dir / "review_note.md",
                )
            )
        except Exception:
            logging.getLogger("ifta.web.worker").exception(
                "admin notify (success) raised for %s", sub.id
            )

    def on_failure(sub: Submission, error: str) -> None:
        email_client.send_failure(sub, error)
        try:
            notifier.send(
                format_event(
                    headline="❌ IFTA submission failed",
                    source="web intake",
                    customer=sub.email,
                    quarter=sub.quarter,
                    extras={
                        "Company": sub.company or "—",
                        "Submission": sub.id,
                        "Error": error,
                    },
                )
            )
        except Exception:
            logging.getLogger("ifta.web.worker").exception(
                "admin notify (failure) raised for %s", sub.id
            )

    console.print("[bold]Starting IFTA worker[/]")
    console.print(f"  db:           {db_path}")
    console.print(f"  submissions:  {submissions_dir}")
    console.print(f"  email:        {'enabled' if email_client.config.enabled else 'disabled (no RESEND_API_KEY)'}")
    console.print(f"  poll:         {poll_interval}s")
    console.print("  stop:         Ctrl-C")

    if once:
        sub = worker_module.process_one_job(
            db_path, submissions_dir, on_success=on_success, on_failure=on_failure
        )
        if sub is None:
            console.print("[dim]queue empty[/]")
        else:
            console.print(f"  processed {sub.id} → {sub.status.value}")
        return

    worker_module.run_forever(
        db_path,
        submissions_dir,
        poll_interval_seconds=poll_interval,
        on_success=on_success,
        on_failure=on_failure,
    )


@main.command(name="telegram-bot")
def telegram_bot() -> None:
    """Run the operator approval bot (approve/reject web submissions)."""
    from ifta.telegram_bot import load_bot_config, run_polling

    try:
        config = load_bot_config(PROJECT_ROOT)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from e

    console.print("[bold]Starting IFTA approval bot[/]")
    console.print(f"  project: {_display_path(PROJECT_ROOT)}")
    console.print(f"  db:      {_display_path(config.db_path)}")
    admins = ", ".join(str(uid) for uid in config.admin_user_ids) or "[red]none set[/]"
    console.print(f"  admins:  {admins}")
    console.print("  stop:    Ctrl-C")
    run_polling(config)


@main.command(name="eval")
@click.option(
    "--cases-dir",
    "cases_dir",
    default=None,
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    help="Directory of *.json eval cases. Defaults to ./evals/cases/.",
)
@click.option(
    "--case",
    "case_filter",
    default=None,
    help="Run only the case with this name (matches the 'name' field).",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Print full agent response for every case (not just failures).",
)
def eval_cmd(cases_dir: Path | None, case_filter: str | None, verbose: bool) -> None:
    """Run the agent eval suite — grade cases under evals/cases/.

    Each case runs the agent (review or ask) and asserts properties of the
    response (must_mention, must_not_mention, total_tax_due, structural).
    Use this before/after prompt or tool changes to catch regressions.
    """
    from ifta.eval import load_cases, run_case

    cases = load_cases(cases_dir)
    if case_filter:
        cases = [c for c in cases if c.name == case_filter]
    if not cases:
        raise click.ClickException("No eval cases found.")

    console.rule(f"[bold]IFTA Eval — {len(cases)} case(s)")
    total_pass = total_fail = 0
    total_cost = 0.0
    total_time = 0.0

    for case in cases:
        console.print(f"\n[bold]▸ {case.name}[/]  [dim]({case.description})[/]")
        result = run_case(case)
        if result.error:
            total_fail += 1
            console.print(f"  [red]✗ ERROR[/] {result.error}")
            continue

        for a in result.assertions:
            sym = "[green]✓[/]" if a.passed else "[red]✗[/]"
            console.print(f"  {sym} {a.name}" + (f"  [dim]{a.detail}[/]" if a.detail else ""))

        if result.passed:
            total_pass += 1
        else:
            total_fail += 1

        if result.metrics is not None:
            total_cost += result.metrics.estimated_cost_usd
            total_time += result.metrics.wall_time_seconds
            console.print(
                f"  [dim]cost=${result.metrics.estimated_cost_usd:.4f}  "
                f"time={result.metrics.wall_time_seconds:.1f}s  "
                f"tools={result.metrics.n_model_calls} calls[/]"
            )

        if verbose or not result.passed:
            preview = result.response_text[:600]
            console.print(f"  [dim]response preview:[/]\n  {preview}…")

    console.rule("[bold]Eval summary")
    sym = "[green]" if total_fail == 0 else "[red]"
    console.print(
        f"{sym}{total_pass} passed · {total_fail} failed[/]  "
        f"[dim]· total cost=${total_cost:.4f}  total time={total_time:.1f}s[/]"
    )
    if total_fail:
        raise click.exceptions.Exit(code=1)


@main.command()
def clients() -> None:
    """List all registered clients in data/clients/."""
    registry = load_registry(PROJECT_ROOT)
    if not registry:
        console.print("[yellow]No clients registered.[/] Add one with `ifta onboard <id>`.")
        return
    table = Table(title="Registered clients")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Base")
    table.add_column("Portal")
    table.add_column("Active")
    table.add_column("Aliases")
    for rec in registry.values():
        table.add_row(
            rec.client_id,
            rec.name,
            rec.base_jurisdiction or "—",
            rec.portal,
            "yes" if rec.active else "no",
            ", ".join(rec.aliases) or "—",
        )
    console.print(table)


@main.command()
@click.argument("client_id")
@click.option("--name", default=None, help="Display name. Defaults to the id, upper-cased.")
@click.option("--base-state", default=None, help="Base jurisdiction (2-letter), e.g. KY.")
@click.option(
    "--portal",
    default="generic",
    type=click.Choice(["generic", "cdtfa", "ky"]),
    show_default=True,
)
@click.option("--alias", "aliases", multiple=True, help="Repeat to add multiple aliases.")
@click.option(
    "--source-folder",
    default=None,
    help="Path to raw source files folder, relative to data/clients/<id>/.",
)
@click.option(
    "--make-inbox/--no-make-inbox",
    default=True,
    show_default=True,
    help="Also scaffold inbox/<client_id>/.",
)
def onboard(
    client_id: str,
    name: str | None,
    base_state: str | None,
    portal: str,
    aliases: tuple[str, ...],
    source_folder: str | None,
    make_inbox: bool,
) -> None:
    """Scaffold a new client: data/clients/<id>/{client.json, profile.json}.

    Example:
        ifta onboard abc_trucking --name "ABC TRUCKING LLC" --base-state TX \\
            --alias abc --alias "abc trucking"
    """
    from ifta.client import ScaffoldError, scaffold_client

    try:
        result = scaffold_client(
            PROJECT_ROOT,
            client_id,
            name=name,
            base_state=base_state,
            portal=portal,
            aliases=aliases,
            source_folder=source_folder,
            make_inbox=make_inbox,
        )
    except ScaffoldError as e:
        raise click.ClickException(str(e)) from e

    if result.dropped_chars:
        console.print(
            f"[yellow]⚠[/] Normalized {client_id!r} → {result.client_id!r} "
            f"(dropped: {result.dropped_chars!r}). Pass --alias {client_id!r} "
            "if you want the original name to resolve too."
        )

    console.print(f"[green]✓[/] Client '{result.client_id}' scaffolded:")
    console.print(f"  data/clients/{result.client_id}/client.json")
    console.print(
        f"  data/clients/{result.client_id}/profile.json  "
        "[dim](stub — fill in once you have data)[/]"
    )
    if result.inbox_dir is not None:
        console.print(f"  inbox/{result.client_id}/")
    console.print(
        f"\n[dim]Next: drop raw files into inbox/{result.client_id}/Q<n>-YYYY/, "
        f"then run `ifta run --client {result.client_id} --quarter Q<n>-YYYY`.[/]"
    )
