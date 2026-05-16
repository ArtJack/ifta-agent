"""Tools the IFTA agent can call.

Each `@beta_tool`-decorated function becomes a tool the Claude SDK exposes
to the model. Schemas are inferred from type hints + docstrings, so keep
both crisp.

The functions all return strings (JSON or plain text) — the SDK feeds the
return value back to the model as the tool result.
"""

from __future__ import annotations

import json
from pathlib import Path

from anthropic import beta_tool

from ifta.calc import IftaReturn, compute_per_truck_lines, compute_return
from ifta.client import (
    ClientContext,
    get_client_record,
    load_client_context,
    load_registry,
    quarter_key,
    resolve_inbox,
)
from ifta.ingest import ingest_folder
from ifta.models import CleanData
from ifta.preflight import preflight_inputs
from ifta.rates import RateTable, fetch_rates
from ifta.validator import Finding, format_findings, load_kb, validate

PROJECT_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_quarter_full(
    quarter: str, client: str | None = None
) -> tuple[CleanData, RateTable, IftaReturn, list[Finding], ClientContext]:
    """Re-ingest + compute. Returns all the intermediate objects too."""
    qkey = quarter_key(quarter)
    inbox = resolve_inbox(PROJECT_ROOT, qkey, client)
    if not inbox.exists():
        raise FileNotFoundError(f"No inbox folder for {qkey} at {inbox}")
    client_context = load_client_context(PROJECT_ROOT, qkey, client=client, inbox=inbox)
    data = ingest_folder(inbox)
    rates = fetch_rates(qkey)
    ret = compute_return(data, rates)
    findings = validate(data, ret)
    return data, rates, ret, findings, client_context


def _load_quarter(
    quarter: str, client: str | None = None
) -> tuple[IftaReturn, list[Finding], ClientContext]:
    """Compact form: just the return, findings, and client context."""
    _, _, ret, findings, ctx = _load_quarter_full(quarter, client)
    return ret, findings, ctx


# ---------------------------------------------------------------------------
# Pipeline tools — current-quarter computation
# ---------------------------------------------------------------------------


@beta_tool
def list_quarters() -> str:
    """List all quarters that have raw files in the inbox/ folder."""
    inbox_root = PROJECT_ROOT / "inbox"
    if not inbox_root.exists():
        return "No inbox/ folder found."
    quarters = sorted(
        p.name for p in inbox_root.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    return "Available quarters: " + ", ".join(quarters) if quarters else "Inbox is empty."


@beta_tool
def get_client_context(quarter: str, client: str | None = None) -> str:
    """Return saved client metadata for a quarter.

    Args:
        quarter: Quarter identifier, e.g. "Q4-2025" or "Q1-2026".
        client: Optional client id/name when using inbox/<client>/<quarter>/.
    """
    qkey = quarter_key(quarter)
    inbox = resolve_inbox(PROJECT_ROOT, qkey, client)
    context = load_client_context(PROJECT_ROOT, qkey, client=client, inbox=inbox)
    return json.dumps(
        {"quarter": qkey, "inbox": str(inbox), "client_context": context.to_prompt_dict()},
        indent=2,
    )


@beta_tool
def query_return(quarter: str, client: str | None = None) -> str:
    """Return the computed IFTA return for a given quarter as JSON.

    Includes fleet miles/gallons/MPG, total tax due, per-truck summary,
    per-jurisdiction lines (including surcharge lines).

    Args:
        quarter: Quarter identifier, e.g. "Q4-2025" or "Q1-2026".
        client: Optional client id/name when using inbox/<client>/<quarter>/.
    """
    ret, _, client_context = _load_quarter(quarter, client)
    return json.dumps(
        {
            "quarter": ret.quarter,
            "fuel": ret.fuel,
            "client_context": client_context.to_prompt_dict(),
            "rate_source_quarter": ret.rate_source_quarter,
            "rate_fallback_used": ret.rate_fallback_used,
            "rate_warning": ret.rate_warning,
            "fleet_miles": round(ret.fleet_miles, 2),
            "fleet_gallons": round(ret.fleet_gallons, 3),
            "fleet_mpg": round(ret.fleet_mpg, 4),
            "total_tax_due": round(ret.total_tax_due, 2),
            "trucks": [
                {
                    "truck_id": t.truck_id,
                    "miles": round(t.miles, 2),
                    "gallons": round(t.gallons, 3),
                    "mpg": round(t.mpg, 4),
                }
                for t in ret.trucks
            ],
            "lines": [
                {
                    "state": ln.state,
                    "is_surcharge": ln.is_surcharge,
                    "miles": int(round(ln.miles)),
                    "tax_paid_gal": int(round(ln.tax_paid_gal)),
                    "taxable_gal": int(round(ln.taxable_gal)),
                    "net_taxable_gal": int(round(ln.net_taxable_gal)),
                    "rate": round(ln.rate, 4),
                    "tax_due": round(ln.tax_due, 2),
                }
                for ln in ret.lines
            ],
        },
        indent=2,
    )


@beta_tool
def inspect_raw_inputs(quarter: str, client: str | None = None) -> str:
    """Inspect the RAW inbox files for a quarter — before compute runs.

    Returns file metadata (names, formats, sheets/headers), parsed row
    counts, the truck IDs the parser found in miles vs fuel, and any
    structural findings (missing files, unparseable rows, truck-ID
    mismatches between miles and fuel, etc.).

    Use this when the user asks "what did I get from the customer?" or
    "is this data even processable?" — before assuming the computed
    numbers are right.

    Args:
        quarter: Quarter identifier, e.g. "Q2-2026".
        client: Optional client id/name when using inbox/<client>/<quarter>/.
    """
    qkey = quarter_key(quarter)
    inbox = resolve_inbox(PROJECT_ROOT, qkey, client)
    report = preflight_inputs(inbox)
    return json.dumps(report.to_dict(), indent=2)


@beta_tool
def query_findings(quarter: str, client: str | None = None) -> str:
    """Return validator findings (errors/warnings/info) for a quarter.

    Args:
        quarter: Quarter identifier, e.g. "Q4-2025".
        client: Optional client id/name when using inbox/<client>/<quarter>/.
    """
    _, findings, _ = _load_quarter(quarter, client)
    return format_findings(findings) if findings else "No findings — return looks clean."


@beta_tool
def query_per_truck(
    quarter: str,
    truck_id: str | None = None,
    client: str | None = None,
) -> str:
    """Return per-truck Jurisdiction Summary data (the same view shown in
    each truck's Excel deliverable). One truck's per-state credit/liability
    may not match the fleet line for that state; sums across all trucks
    reconcile to the fleet filing.

    Args:
        quarter: Quarter identifier, e.g. "Q4-2025".
        truck_id: Optional. If omitted, returns ALL trucks. If given,
            returns only that truck's lines. Case-sensitive match.
        client: Optional client id/name when using inbox/<client>/<quarter>/.
    """
    data, rates, ret, _, client_context = _load_quarter_full(quarter, client)
    per_truck = compute_per_truck_lines(data, ret, rates)

    if truck_id and truck_id not in per_truck:
        return (
            f"Truck '{truck_id}' not found for {ret.quarter}. "
            f"Available trucks: {list(per_truck.keys())}"
        )

    def _serialize(tid: str) -> dict[str, object]:
        lines = per_truck[tid]
        return {
            "truck_id": tid,
            "driver": data.driver(tid),
            "card_number": data.card(tid),
            "total_miles": int(round(sum(ln.miles for ln in lines if not ln.is_surcharge))),
            "total_tax_paid_gal": int(
                round(sum(ln.tax_paid_gal for ln in lines if not ln.is_surcharge))
            ),
            "total_taxable_gal": int(
                round(sum(ln.taxable_gal for ln in lines if not ln.is_surcharge))
            ),
            "share_of_tax_due": round(sum(ln.tax_due for ln in lines), 2),
            "lines": [
                {
                    "state": ln.state,
                    "is_surcharge": ln.is_surcharge,
                    "miles": int(round(ln.miles)),
                    "tax_paid_gal": int(round(ln.tax_paid_gal)),
                    "taxable_gal": int(round(ln.taxable_gal)),
                    "net_taxable_gal": int(round(ln.net_taxable_gal)),
                    "rate": round(ln.rate, 4),
                    "tax_due": round(ln.tax_due, 2),
                }
                for ln in lines
            ],
        }

    payload: dict[str, object] = {
        "quarter": ret.quarter,
        "client_context": client_context.to_prompt_dict(),
        "fleet_mpg": round(ret.fleet_mpg, 4),
        "note": (
            "Per-truck figures use the FLEET MPG and per-state rates. Each "
            "truck's per-state line shows that truck's contribution to the "
            "fleet's IFTA filing; sums across all trucks reconcile to the "
            "fleet total."
        ),
    }
    if truck_id:
        payload["truck"] = _serialize(truck_id)
    else:
        payload["trucks"] = [_serialize(tid) for tid in per_truck]
    return json.dumps(payload, indent=2)


@beta_tool
def compare_to_filing(quarter: str, expected_total: float, client: str | None = None) -> str:
    """Compare the pipeline's computed total tax due against an expected
    figure (e.g. from a draft return). Returns the difference.

    Args:
        quarter: Quarter to compute.
        expected_total: Dollar amount you expect.
        client: Optional client id/name when using inbox/<client>/<quarter>/.
    """
    ret, _, client_context = _load_quarter(quarter, client)
    diff = round(ret.total_tax_due - expected_total, 2)
    return json.dumps(
        {
            "client_context": client_context.to_prompt_dict(),
            "computed_total": round(ret.total_tax_due, 2),
            "expected_total": expected_total,
            "difference": diff,
            "matches": abs(diff) < 0.01,
        }
    )


# ---------------------------------------------------------------------------
# Rules tools
# ---------------------------------------------------------------------------


@beta_tool
def lookup_rate(state: str, quarter: str, fuel: str = "diesel") -> str:
    """Look up the IFTA tax rate (base + surcharge) for a state/quarter/fuel.

    Args:
        state: 2-letter state/province code, e.g. "CA", "KY", "ON".
        quarter: Quarter identifier, e.g. "Q1-2026".
        fuel: Fuel type. Default "diesel". Other options: gasoline, gasohol,
            propane, lng, cng, ethanol, methanol, e85, biodiesel, electricity,
            hydrogen.
    """
    rates = fetch_rates(quarter, fuel=fuel)
    base = rates.get(state.upper())
    sur = rates.surcharge(state.upper())
    return json.dumps(
        {
            "state": state.upper(),
            "requested_quarter": rates.requested_quarter,
            "source_quarter": rates.source_quarter,
            "fallback_used": rates.fallback_used,
            "warning": rates.warning,
            "fuel": rates.fuel,
            "base_rate_usd_per_gallon": base,
            "surcharge_rate_usd_per_gallon": sur if sur > 0 else None,
            "total_effective_rate": base + sur,
        }
    )


@beta_tool
def get_regulations(topic: str | None = None) -> str:
    """Look up IFTA regulations. Call without a topic for the full KB, or
    pass a topic key to filter.

    Args:
        topic: Optional key (filing_deadlines, penalties_and_interest,
            base_state_rules, fleet_mpg_calculation, surcharge_states,
            special_states, recordkeeping, common_errors_and_fixes,
            kentucky_dor_filing, cdtfa_california_filing, etc.).
    """
    kb = load_kb()
    if topic and topic in kb:
        return json.dumps({topic: kb[topic]}, indent=2)
    if topic:
        topics = ", ".join(k for k in kb if not k.startswith("_"))
        return f"Unknown topic '{topic}'. Available: {topics}"
    return json.dumps(kb, indent=2)


# ---------------------------------------------------------------------------
# Generic client tools — work for any registered client
# ---------------------------------------------------------------------------


@beta_tool
def list_clients() -> str:
    """List all clients registered in data/clients/. Returns id, name, base
    state, portal, and whether the client is active."""
    registry = load_registry(PROJECT_ROOT)
    if not registry:
        return "No clients registered. Add one with `ifta onboard <client_id>`."
    rows = [
        {
            "client_id": rec.client_id,
            "name": rec.name,
            "base_jurisdiction": rec.base_jurisdiction,
            "portal": rec.portal,
            "active": rec.active,
            "aliases": list(rec.aliases),
        }
        for rec in registry.values()
    ]
    return json.dumps(rows, indent=2)


def _require_client(client_id: str) -> tuple[ClientContext, object]:
    """Look up a registered client, return (synthetic_context, record) or raise."""
    rec = get_client_record(PROJECT_ROOT, client_id)
    if rec is None:
        raise FileNotFoundError(
            f"Client '{client_id}' not registered. Known: "
            f"{list(load_registry(PROJECT_ROOT).keys())}"
        )
    return None, rec  # type: ignore[return-value]


@beta_tool
def get_client_profile(client_id: str) -> str:
    """Return the operating profile (fleet evolution, fuel vendors, routes,
    MPG/miles/tax history, comparison thresholds, narrative) for a registered
    client. Always call this before reasoning about a specific client.

    Args:
        client_id: Registry id or alias (e.g. "dm_express", "david", "menshikov").
    """
    _, rec = _require_client(client_id)
    p = rec.resolve_path("profile_path")  # type: ignore[union-attr]
    if p is None or not p.exists():
        return f"No profile.json on disk for client '{client_id}'."
    return p.read_text(encoding="utf-8")


@beta_tool
def query_client_history(client_id: str, quarter: str | None = None) -> str:
    """Look up a registered client's historical filings (parsed from prior
    returns / spreadsheets).

    Args:
        client_id: Registry id or alias.
        quarter: Optional quarter label, e.g. "Q4 2025" or "Q4-2025". Omit for
            a summary across all quarters.
    """
    _, rec = _require_client(client_id)
    p = rec.resolve_path("history_path")  # type: ignore[union-attr]
    if p is None or not p.exists():
        return f"No history.json on disk for client '{client_id}'."
    history = json.loads(p.read_text(encoding="utf-8"))

    # History is either a list (Menshikov style) or a dict keyed by quarter
    # (DM Express style). Handle both.
    if isinstance(history, list):
        if quarter:
            q_norm = quarter.upper().replace("-", " ").strip()
            matches = [
                f
                for f in history
                if f"{f.get('quarter', '').upper()} {f.get('year', '')}".strip() == q_norm
                or f.get("quarter", "").upper() == q_norm
            ]
            return json.dumps(matches, indent=2)
        summary = [
            {
                "quarter": f.get("quarter"),
                "year": f.get("year"),
                "total_due": f.get("total_due_or_credit"),
                "total_miles": f.get("total_miles"),
                "fleet_mpg": f.get("fleet_mpg"),
                "jurisdictions_count": len(f.get("jurisdictions") or []),
            }
            for f in history
        ]
        return json.dumps(summary, indent=2)

    if quarter:
        norm = quarter.replace("-", " ").strip()
        if norm in history:
            return json.dumps(history[norm], indent=2)
        for k in history:
            if k.replace(" ", "").lower() == norm.replace(" ", "").lower():
                return json.dumps(history[k], indent=2)
        return f"Quarter '{quarter}' not in history. Available: {list(history.keys())}"
    summary = {
        q: {
            "trucks": data.get("trucks"),
            "states_count": len(data.get("states", [])),
            "fleet_miles": data.get("fleet_miles"),
            "fleet_mpg": data.get("fleet_mpg"),
            "fuel_vendors": (data.get("fuel_vendor_breakdown") or {}).get("vendors", []),
        }
        for q, data in history.items()
    }
    return json.dumps(summary, indent=2)


@beta_tool
def list_client_files(client_id: str) -> str:
    """List every file in the registered client's source folder with a short
    description (xlsx sheets, CSVs, PDFs). Use this to find the raw inputs
    the customer sent.

    Args:
        client_id: Registry id or alias.
    """
    _, rec = _require_client(client_id)
    folder = rec.resolve_path("source_folder")  # type: ignore[union-attr]
    if folder is None:
        return f"Client '{client_id}' has no source_folder configured."
    if not folder.exists():
        return f"Source folder not found at {folder}."

    import pandas as pd

    items: list[str] = []
    for p in sorted(folder.iterdir()):
        if p.name.startswith(".") or not p.is_file():
            continue
        suffix = p.suffix.lower()
        if suffix == ".xlsx":
            try:
                xl = pd.ExcelFile(p)
                items.append(f"  - {p.name} (xlsx) — sheets: {xl.sheet_names}")
            except Exception as e:
                items.append(f"  - {p.name} (xlsx) — couldn't read: {e}")
        else:
            items.append(f"  - {p.name} ({suffix.lstrip('.')})")
    return f"Source files for {rec.name}:\n" + "\n".join(items)  # type: ignore[union-attr]


@beta_tool
def compare_quarter_to_history(quarter: str, client: str | None = None) -> str:
    """Compare a computed quarter against the active client's historical
    pattern. Reads comparison_thresholds from the client's profile.json (MPG
    range + tolerance, miles range + low_threshold, tax range + tolerance).

    Args:
        quarter: Quarter to evaluate, e.g. "Q1-2026".
        client: Optional client id/name; if omitted, uses the inbox client.json.
    """
    ret, _, client_context = _load_quarter(quarter, client)
    if client_context.is_unknown or client_context.record_dir is None:
        return json.dumps(
            {
                "comparison": "not_applicable",
                "reason": (
                    "No client profile is available for this quarter — cannot compare "
                    "against history."
                ),
                "client_context": client_context.to_prompt_dict(),
            },
            indent=2,
        )

    rec = get_client_record(PROJECT_ROOT, client_context.client_id)
    if rec is None:
        return json.dumps(
            {
                "comparison": "not_applicable",
                "reason": f"Client '{client_context.client_id}' is not in the registry.",
            },
            indent=2,
        )
    profile_p = rec.resolve_path("profile_path")
    if profile_p is None or not profile_p.exists():
        return f"No profile.json for client '{client_context.client_id}'."
    profile = json.loads(profile_p.read_text(encoding="utf-8"))
    thresholds = profile.get("comparison_thresholds")
    if not thresholds:
        return json.dumps(
            {
                "comparison": "not_applicable",
                "reason": (
                    f"Client '{client_context.client_id}' profile has no "
                    "comparison_thresholds block. Add one to enable anomaly detection."
                ),
                "client_context": client_context.to_prompt_dict(),
            },
            indent=2,
        )

    mpg = thresholds.get("fleet_mpg") or {}
    miles = thresholds.get("fleet_miles") or {}
    tax = thresholds.get("total_tax_due") or {}

    flags: list[str] = []
    if "min" in mpg and ret.fleet_mpg < mpg["min"] - mpg.get("tolerance", 0):
        flags.append(
            f"Fleet MPG {ret.fleet_mpg:.2f} is below historical floor of {mpg['min']}."
        )
    if "max" in mpg and ret.fleet_mpg > mpg["max"] + mpg.get("tolerance", 0):
        flags.append(
            f"Fleet MPG {ret.fleet_mpg:.2f} is above historical ceiling of {mpg['max']}."
        )
    if "low_threshold" in miles and ret.fleet_miles < miles["low_threshold"]:
        flags.append(
            f"Fleet miles {ret.fleet_miles:,.0f} is unusually low (below "
            f"{miles['low_threshold']:,}). Possible downtime or partial data."
        )
    if "max" in miles and ret.fleet_miles > miles["max"]:
        flags.append(
            f"Fleet miles {ret.fleet_miles:,.0f} exceeds historical max of "
            f"{miles['max']:,}."
        )
    if "max" in tax and ret.total_tax_due > tax["max"] + tax.get("tolerance", 0):
        flags.append(
            f"Total tax due ${ret.total_tax_due:,.2f} exceeds historical max of "
            f"${tax['max']:.2f}."
        )
    if ret.total_tax_due < 0:
        flags.append(
            f"Net credit ${ret.total_tax_due:,.2f} — confirm this is a true credit, "
            "not a data issue."
        )

    return json.dumps(
        {
            "quarter": quarter,
            "client_id": client_context.client_id,
            "computed": {
                "fleet_mpg": round(ret.fleet_mpg, 2),
                "fleet_miles": round(ret.fleet_miles),
                "total_tax_due": round(ret.total_tax_due, 2),
            },
            "historical_norms": {
                "mpg_range": [mpg.get("min"), mpg.get("max")],
                "miles_range": [miles.get("min"), miles.get("max")],
                "tax_range": [tax.get("min"), tax.get("max")],
            },
            "anomalies": flags or ["No anomalies — within historical norms."],
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# Filing-archive tools — generic, work for any client with PDF returns in
# their source_folder.
# ---------------------------------------------------------------------------


@beta_tool
def list_past_filings(client_id: str) -> str:
    """List historical filed-return PDFs in the registered client's
    source_folder.

    Args:
        client_id: Registry id or alias.
    """
    _, rec = _require_client(client_id)
    folder = rec.resolve_path("source_folder")  # type: ignore[union-attr]
    if folder is None:
        return f"Client '{client_id}' has no source_folder configured."
    if not folder.exists():
        return f"Source folder not found at {folder}."
    pdfs = sorted(p.name for p in folder.glob("*.pdf"))
    if not pdfs:
        return f"No PDF filings in {folder}."
    return f"Past filings for {rec.name}:\n" + "\n".join(  # type: ignore[union-attr]
        f"  - {p}" for p in pdfs
    )


@beta_tool
def read_past_filing(client_id: str, filename: str) -> str:
    """Read the text of a historical filed-return PDF from a client's
    source_folder.

    Args:
        client_id: Registry id or alias.
        filename: Exact PDF filename (call list_past_filings to see options).
    """
    import pdfplumber

    _, rec = _require_client(client_id)
    folder = rec.resolve_path("source_folder")  # type: ignore[union-attr]
    if folder is None:
        return f"Client '{client_id}' has no source_folder configured."
    path = folder / filename
    if not path.exists():
        return f"File not found: {path}"

    parts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            parts.append(f"--- Page {i} ---\n{page.extract_text() or '[no text]'}")
    text = "\n\n".join(parts)
    if len(text) > 8000:
        text = text[:8000] + "\n\n[... truncated ...]"
    return text


# ---------------------------------------------------------------------------
# The list passed to the SDK
# ---------------------------------------------------------------------------


ALL_TOOLS = [
    list_quarters,
    get_client_context,
    inspect_raw_inputs,
    query_return,
    query_findings,
    query_per_truck,
    compare_to_filing,
    lookup_rate,
    get_regulations,
    list_clients,
    get_client_profile,
    query_client_history,
    list_client_files,
    compare_quarter_to_history,
    list_past_filings,
    read_past_filing,
]
