"""One deterministic path from an inbox folder to a gated IFTA return.

Every customer-facing entry point — the web worker, the Telegram bot, and the
`ifta run` / `ifta deliver` CLI commands — needs the same sequence:

    preflight → ingest (honoring preflight's dedup) → rates → compute →
    validate → deterministic filing gate

Each used to open-code that sequence, and they drifted. Three separate bugs
came from exactly that drift: only the web path passed preflight's
``skipped_files`` to the ingester (so the other paths silently double-counted a
duplicate export), only the agent path consulted the filing gate (so a packet
with error-level findings shipped as "ready" whenever the AI review was
skipped), and the Telegram path recomputed readiness with its own inline
boolean. Adding a gate in one place left the others exposed.

`compute_quarter()` is now the single implementation. A check added here
applies to every path that can reach a customer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ifta.calc import IftaReturn, compute_return
from ifta.ingest import ingest_folder
from ifta.models import CleanData
from ifta.preflight import PreflightReport, format_preflight, preflight_inputs
from ifta.rates import RateTable, fetch_rates
from ifta.review_packet import determine_filing_status
from ifta.validator import Finding, validate

NO_DATA_MESSAGE = (
    "No usable data parsed from the uploaded files. "
    "Expected mileage by truck/state and fuel by truck/state."
)


class QuarterBlockedError(Exception):
    """The inbox can't produce a return at all (bad files, nothing parsable).

    Distinct from a computed return that must not be *filed* — that is
    `ComputedQuarter.blocked`, which still carries real numbers a human can
    inspect. This exception means there is nothing to compute.
    """

    def __init__(self, message: str, *, report: PreflightReport | None = None) -> None:
        super().__init__(message)
        self.report = report


@dataclass(frozen=True)
class ComputedQuarter:
    """A computed quarter plus the deterministic decision about filing it."""

    quarter: str
    inbox: Path
    preflight: PreflightReport
    data: CleanData
    rates: RateTable
    ret: IftaReturn
    findings: list[Finding]

    @property
    def filing_status(self) -> dict[str, Any]:
        """The authoritative gate: DO_NOT_FILE / READY_WITH_WARNINGS / READY_TO_FILE."""
        return determine_filing_status(self.ret, self.findings)

    @property
    def status(self) -> str:
        return str(self.filing_status["status"])

    @property
    def blocked(self) -> bool:
        """True when this return must not be filed as-is."""
        return self.status == "DO_NOT_FILE"

    @property
    def block_reasons(self) -> list[str]:
        return list(self.filing_status["reasons"])

    @property
    def has_errors(self) -> bool:
        return any(f.severity == "error" for f in self.findings)


def compute_quarter(
    inbox: Path,
    quarter: str,
    *,
    fuel: str = "diesel",
    refresh_rates: bool = False,
    preflight: PreflightReport | None = None,
    ignore_preflight_errors: bool = False,
    require_data: bool = True,
    preflight_error_prefix: str = "Preflight found ERROR-level issues in your uploaded files:\n",
) -> ComputedQuarter:
    """Run the deterministic pipeline for one quarter's inbox.

    Args:
        inbox: Folder holding the raw customer files.
        quarter: Quarter identifier, e.g. "Q2-2026".
        fuel: Fuel type for the rate matrix.
        refresh_rates: Re-fetch the rate matrix even if cached.
        preflight: An already-computed preflight report. Pass this when the
            caller needed the report first (to show it, or to run a client
            identity check) so the inbox isn't parsed twice.
        ignore_preflight_errors: Compute even when preflight reports ERRORs.
            The resulting return is still gated normally.
        require_data: Raise when nothing parsable came out of the inbox. Set
            False for inspection callers (agent tools, `ifta ask`) that would
            rather report an empty return than fail.
        preflight_error_prefix: Leading text for the raised message, so each
            caller keeps its own customer-facing wording.

    Raises:
        QuarterBlockedError: the inbox is missing, preflight found blocking errors,
            or nothing parsable came out of the files.
    """
    if not inbox.exists():
        raise QuarterBlockedError(f"inbox not found: {inbox}")

    report = preflight if preflight is not None else preflight_inputs(inbox)
    if report.has_errors and not ignore_preflight_errors:
        raise QuarterBlockedError(preflight_error_prefix + format_preflight(report), report=report)

    # skip_files honors preflight's auto-dedup. Preflight has already told the
    # customer which duplicate export it would skip; ingesting it anyway sums
    # both copies (coalesce_* adds same-(truck, state) rows) and doubles the
    # miles or gallons behind every number on the return.
    data = ingest_folder(inbox, skip_files=set(report.skipped_files))
    if require_data and not data.miles and not data.fuel:
        raise QuarterBlockedError(NO_DATA_MESSAGE, report=report)

    rates = fetch_rates(quarter, fuel=fuel, force=refresh_rates)
    ret = compute_return(data, rates)
    findings = validate(data, ret)

    return ComputedQuarter(
        quarter=quarter,
        inbox=inbox,
        preflight=report,
        data=data,
        rates=rates,
        ret=ret,
        findings=findings,
    )
