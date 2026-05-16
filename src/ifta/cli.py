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
    reload_registry,
    resolve_inbox,
    resolve_output_dir,
)
from ifta.ingest import ingest_folder
from ifta.rates import fetch_rates
from ifta.report import write_cleaned_csvs, write_per_truck_filings, write_portal_csv
from ifta.validator import format_findings, validate

console = Console()
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


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
    qkey = quarter_key(quarter)
    inbox = inbox or resolve_inbox(PROJECT_ROOT, qkey, client)
    out_dir = out_dir or resolve_output_dir(PROJECT_ROOT, qkey, client)
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
    console.print(f"  ✓ {portal_csv.relative_to(PROJECT_ROOT)}")
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
        console.print(f"  ✓ {tp.relative_to(PROJECT_ROOT)}")
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
    default="claude-opus-4-7",
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

    qkey = quarter_key(quarter)
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
    console.print(f"  ✓ {path.relative_to(PROJECT_ROOT)}")
    console.print(
        f"  [dim]Agent run: {agent_metrics.wall_time_seconds:.1f}s · "
        f"{agent_metrics.output_tokens:,} output tokens · "
        f"${agent_metrics.estimated_cost_usd:.4f}[/]"
    )
    console.rule("Review Note")
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
    default="claude-opus-4-7",
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

    qkey = quarter_key(quarter) if quarter else None
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
    default="claude-opus-4-7",
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
    default="claude-opus-4-7",
    type=click.Choice(MODEL_CHOICES),
    show_default=True,
    help="Agent model — default is Opus (most precise).",
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

    qkey = quarter_key(quarter)
    inbox = resolve_inbox(PROJECT_ROOT, qkey, client)
    out_dir = resolve_output_dir(PROJECT_ROOT, qkey, client)
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
    console.print(f"  ✓ {portal_csv.relative_to(PROJECT_ROOT)}")
    for p in diagnostic_paths:
        console.print(f"  ✓ {p.relative_to(PROJECT_ROOT)} [dim](diagnostic)[/]")

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
            console.print(f"  ✓ {note_path.relative_to(PROJECT_ROOT)}")
            console.print(
                f"  [dim]Agent: {agent_metrics.wall_time_seconds:.1f}s · "
                f"{agent_metrics.input_tokens + agent_metrics.cache_read_tokens + agent_metrics.cache_creation_tokens:,} in / "
                f"{agent_metrics.output_tokens:,} out tokens · "
                f"${agent_metrics.estimated_cost_usd:.4f}[/]"
            )

            console.rule("[bold green]Agent Review")
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
        console.print(f"  ✓ {tp.relative_to(PROJECT_ROOT)}")

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
    console.print(f"[dim]All files in: {out_dir.relative_to(PROJECT_ROOT)}/[/]")

    # --- 6. Open in Finder (macOS) ---
    if not no_open:
        import contextlib

        with contextlib.suppress(FileNotFoundError):
            subprocess.run(["open", str(out_dir)], check=False)


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
    import json
    import re

    norm = re.sub(r"[^a-z0-9]+", "_", client_id.strip().lower()).strip("_")
    if not norm:
        raise click.ClickException("client_id must contain at least one alphanumeric character.")

    client_dir = PROJECT_ROOT / "data" / "clients" / norm
    if client_dir.exists() and (client_dir / "client.json").exists():
        raise click.ClickException(
            f"Client '{norm}' already exists at {client_dir}. "
            "Edit client.json directly to update."
        )
    client_dir.mkdir(parents=True, exist_ok=True)

    display_name = name or norm.replace("_", " ").upper()
    client_meta = {
        "client_id": norm,
        "name": display_name,
        "aliases": list(aliases),
        "base_jurisdiction": (base_state or "").upper() or None,
        "portal": portal,
        "profile": norm,
        "source_folder": source_folder,
        "profile_path": "profile.json",
        "history_path": "history.json",
        "active": True,
        "notes": f"Onboarded via `ifta onboard {norm}`.",
    }
    (client_dir / "client.json").write_text(
        json.dumps(client_meta, indent=2) + "\n", encoding="utf-8"
    )

    profile_stub = {
        "operator": display_name,
        "base_state": (base_state or "").upper() or None,
        "portal": portal,
        "fleet": {"trucks": None, "fuel_type": "Diesel"},
        "history_window": {"first_quarter": None, "last_quarter": None, "filings_parsed": 0},
        "comparison_thresholds": {
            "fleet_mpg": {"min": 0, "max": 99, "tolerance": 0.5},
            "fleet_miles": {"min": 0, "max": 9_999_999, "low_threshold": 0},
            "total_tax_due": {"min": -9999, "max": 999_999, "tolerance": 500},
        },
        "narrative_for_agent": (
            f"New client {display_name}, base state "
            f"{(base_state or '').upper() or 'TBD'}. No history loaded yet — "
            "populate this file after the first filing."
        ),
        "per_quarter_filing_checklist": [
            "Confirm fleet trucks match the IFTA decal list.",
            "Verify base-state-specific items (surcharges, weight-distance taxes).",
            "Cross-check fuel-vendor totals against fuel-card receipts.",
        ],
    }
    (client_dir / "profile.json").write_text(
        json.dumps(profile_stub, indent=2) + "\n", encoding="utf-8"
    )

    if make_inbox:
        (PROJECT_ROOT / "inbox" / norm).mkdir(parents=True, exist_ok=True)

    reload_registry(PROJECT_ROOT)
    console.print(f"[green]✓[/] Client '{norm}' scaffolded:")
    console.print(f"  data/clients/{norm}/client.json")
    console.print(f"  data/clients/{norm}/profile.json  [dim](stub — fill in once you have data)[/]")
    if make_inbox:
        console.print(f"  inbox/{norm}/")
    console.print(
        "\n[dim]Next: drop raw files into inbox/{}/Q<n>-YYYY/, then run "
        "`ifta run --client {} --quarter Q<n>-YYYY`.[/]".format(norm, norm)
    )
