"""Telegram intake bot for IFTA customer files.

The bot is intentionally a thin, controlled shell around the deterministic
pipeline:

1. Authenticate the sender by Telegram numeric user id.
2. Save uploaded files into inbox/<client>/<quarter>/.
3. Run preflight before any calculation.
4. Run the known pipeline functions and agent review.
5. Send the review packet back to the customer.

It does not let the model execute arbitrary code or choose filesystem paths.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ifta.agent import (
    DEFAULT_EFFORT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    AgentMetrics,
    ReviewNote,
    write_review_md,
)
from ifta.agent import (
    review as agent_review,
)
from ifta.calc import compute_per_truck_lines, compute_return
from ifta.client import (
    ClientRecord,
    load_client_context,
    load_registry,
    normalize_client_id,
    quarter_key,
    resolve_output_dir,
)
from ifta.ingest import ingest_folder
from ifta.preflight import PreflightReport, format_preflight, preflight_inputs
from ifta.rates import fetch_rates
from ifta.report import (
    write_cleaned_csvs,
    write_owner_review_xlsx,
    write_per_truck_filings,
    write_portal_csv,
)
from ifta.validator import Finding, format_findings, validate

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUPPORTED_UPLOAD_SUFFIXES = {".csv", ".xlsx", ".xlsm", ".xls", ".pdf"}
TELEGRAM_ACCESS_FILE = Path("data") / "telegram_access.json"


class AuthorizationError(RuntimeError):
    """Raised when a Telegram user is not allowed to access a client."""


class DeliveryBlockedError(RuntimeError):
    """Raised when preflight blocks calculation."""


@dataclass
class BotConfig:
    project_root: Path
    token: str
    admin_user_ids: tuple[int, ...] = ()
    max_file_mb: float = 20.0
    skip_agent: bool = False
    agent_model: str = DEFAULT_MODEL
    agent_effort: str = DEFAULT_EFFORT
    agent_max_tokens: int = DEFAULT_MAX_TOKENS["review"]


@dataclass
class Submission:
    client_id: str
    client_name: str
    quarter: str
    inbox: Path
    out_dir: Path
    uploaded_files: list[str] = field(default_factory=list)

    def to_user_data(self) -> dict[str, object]:
        return {
            "client_id": self.client_id,
            "client_name": self.client_name,
            "quarter": self.quarter,
            "inbox": str(self.inbox),
            "out_dir": str(self.out_dir),
            "uploaded_files": list(self.uploaded_files),
        }

    @classmethod
    def from_user_data(cls, payload: object) -> Submission | None:
        if not isinstance(payload, dict):
            return None
        try:
            return cls(
                client_id=str(payload["client_id"]),
                client_name=str(payload["client_name"]),
                quarter=str(payload["quarter"]),
                inbox=Path(str(payload["inbox"])),
                out_dir=Path(str(payload["out_dir"])),
                uploaded_files=[str(x) for x in payload.get("uploaded_files", [])],
            )
        except KeyError:
            return None


@dataclass
class DeliveryResult:
    client_name: str
    quarter: str
    total_tax_due: float
    fleet_miles: float
    fleet_gallons: float
    fleet_mpg: float
    ready_to_file: bool
    warnings: list[str]
    portal_csv: Path
    review_xlsx: Path
    review_note: Path
    truck_files: list[Path]
    output_dir: Path

    @property
    def customer_files(self) -> list[Path]:
        return [self.portal_csv, self.review_xlsx, self.review_note, *self.truck_files]


@dataclass
class ClientIdentityReport:
    """Deterministic guardrail that the upload belongs to the selected client."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    def format(self) -> str:
        parts: list[str] = []
        if self.errors:
            parts.append("CLIENT IDENTITY ERRORS:")
            parts.extend(f"  [CLIENT_IDENTITY_MISMATCH] {msg}" for msg in self.errors)
        if self.warnings:
            parts.append("CLIENT IDENTITY WARNINGS:")
            parts.extend(f"  [CLIENT_IDENTITY_REVIEW] {msg}" for msg in self.warnings)
        return "\n".join(parts)


def parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_user_ids(value: str | None) -> tuple[int, ...]:
    """Parse comma/space separated Telegram numeric IDs from env/config."""
    if not value:
        return ()
    ids: list[int] = []
    for chunk in re.split(r"[,\s]+", value.strip()):
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError as e:
            raise ValueError(f"Invalid Telegram user id {chunk!r}; expected digits.") from e
    return tuple(ids)


def load_bot_config(project_root: Path = PROJECT_ROOT) -> BotConfig:
    load_dotenv(project_root / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather, then "
            "put TELEGRAM_BOT_TOKEN=<token> in ifta_pipeline/.env."
        )

    max_file_mb = float(os.environ.get("TELEGRAM_MAX_FILE_MB", "20"))
    max_tokens = int(os.environ.get("TELEGRAM_AGENT_MAX_TOKENS", DEFAULT_MAX_TOKENS["review"]))
    return BotConfig(
        project_root=project_root,
        token=token,
        admin_user_ids=parse_user_ids(os.environ.get("TELEGRAM_ADMIN_USER_IDS")),
        max_file_mb=max_file_mb,
        skip_agent=parse_bool(os.environ.get("TELEGRAM_SKIP_AGENT"), default=False),
        agent_model=os.environ.get("TELEGRAM_AGENT_MODEL", DEFAULT_MODEL),
        agent_effort=os.environ.get("TELEGRAM_AGENT_EFFORT", DEFAULT_EFFORT),
        agent_max_tokens=max_tokens,
    )


def telegram_access_path(project_root: Path) -> Path:
    """Local runtime allowlist written by /approve; intentionally git-ignored."""
    return project_root / TELEGRAM_ACCESS_FILE


def load_telegram_access(project_root: Path) -> dict[str, set[int]]:
    """Load local Telegram approvals grouped by client id."""
    path = telegram_access_path(project_root)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    clients_payload = payload.get("clients", payload)
    if not isinstance(clients_payload, dict):
        return {}

    access: dict[str, set[int]] = {}
    for raw_client_id, raw_ids in clients_payload.items():
        if not isinstance(raw_ids, list):
            continue
        client_id = normalize_client_id(str(raw_client_id), project_root)
        ids: set[int] = set()
        for raw_id in raw_ids:
            try:
                ids.add(int(raw_id))
            except (TypeError, ValueError):
                continue
        if ids:
            access[client_id] = ids
    return access


def write_telegram_access(project_root: Path, access: dict[str, set[int]]) -> Path:
    """Persist local Telegram approvals in a stable JSON shape."""
    path = telegram_access_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "clients": {
            client_id: sorted(ids)
            for client_id, ids in sorted(access.items())
            if ids
        }
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def client_telegram_user_ids(project_root: Path, rec: ClientRecord) -> set[int]:
    """All Telegram ids allowed for a client from registry plus local approvals."""
    return set(rec.telegram_user_ids) | load_telegram_access(project_root).get(rec.client_id, set())


def approve_telegram_user(
    *,
    project_root: Path,
    user_id: int,
    requested_client: str,
) -> ClientRecord:
    """Approve a Telegram user for one client through the local runtime allowlist."""
    registry = load_registry(project_root)
    client_id = normalize_client_id(requested_client, project_root)
    rec = registry.get(client_id)
    if rec is None:
        raise AuthorizationError(
            f"Unknown client {requested_client!r}. Registered: {', '.join(registry)}"
        )
    access = load_telegram_access(project_root)
    access.setdefault(rec.client_id, set()).add(user_id)
    write_telegram_access(project_root, access)
    return rec


def clients_for_telegram_user(project_root: Path, user_id: int) -> list[ClientRecord]:
    """Return registered clients that explicitly allow this Telegram user id."""
    return [
        rec
        for rec in load_registry(project_root).values()
        if user_id in client_telegram_user_ids(project_root, rec)
    ]


def resolve_authorized_client(
    *,
    project_root: Path,
    user_id: int,
    requested_client: str | None,
    admin_user_ids: tuple[int, ...],
) -> ClientRecord:
    """Resolve the client this Telegram user may submit for."""
    registry = load_registry(project_root)
    assigned = clients_for_telegram_user(project_root, user_id)
    is_admin = user_id in admin_user_ids

    if requested_client:
        client_id = normalize_client_id(requested_client, project_root)
        rec = registry.get(client_id)
        if rec is None:
            if is_admin:
                raise AuthorizationError(
                    f"Unknown client {requested_client!r}. Registered: {', '.join(registry)}"
                )
            raise AuthorizationError("Unknown or unauthorized client.")
        if is_admin or rec in assigned:
            return rec
        raise AuthorizationError("Your Telegram id is not allowed to submit for that client.")

    if len(assigned) == 1:
        return assigned[0]
    if is_admin:
        raise AuthorizationError(
            "Admin account: pass a client id, e.g. /new Q2-2026 dm_express."
        )
    if not assigned:
        raise AuthorizationError(
            "This Telegram id is not attached to a client yet. Send /id to Eugene."
        )
    names = ", ".join(rec.client_id for rec in assigned)
    raise AuthorizationError(f"Multiple clients allowed ({names}). Pass one after the quarter.")


def authorize_submission_access(
    *,
    project_root: Path,
    user_id: int,
    submission: Submission,
    admin_user_ids: tuple[int, ...],
) -> ClientRecord:
    """Re-check access for an existing upload session before reading/writing files."""
    rec = load_registry(project_root).get(normalize_client_id(submission.client_id, project_root))
    if rec is None:
        raise AuthorizationError("This upload session is for a client that is no longer registered.")
    if user_id in admin_user_ids or user_id in client_telegram_user_ids(project_root, rec):
        return rec
    raise AuthorizationError(
        "This upload session is no longer authorized for your Telegram id. "
        "Send /id to Eugene if access should be restored."
    )


def safe_filename(filename: str) -> str:
    """Return a filesystem-safe upload filename without directory traversal."""
    name = Path(filename or "upload").name.strip()
    stem = Path(name).stem or "upload"
    suffix = Path(name).suffix.lower()
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or "upload"
    return f"{safe_stem}{suffix}"


def unique_destination(folder: Path, filename: str) -> Path:
    candidate = folder / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for i in range(2, 1000):
        alt = folder / f"{stem}_{i}{suffix}"
        if not alt.exists():
            return alt
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return folder / f"{stem}_{ts}{suffix}"


def _has_customer_files(folder: Path) -> bool:
    if not folder.exists():
        return False
    return any(
        p.is_file() and not p.name.startswith(".") and p.name != "client.json"
        for p in folder.iterdir()
    )


def _write_inbox_client_json(folder: Path, rec: ClientRecord, user_id: int) -> None:
    payload = {
        "client_id": rec.client_id,
        "name": rec.name,
        "base_jurisdiction": rec.base_jurisdiction,
        "portal": rec.portal,
        "profile": rec.profile,
        "notes": f"Submitted through Telegram by user_id={user_id}.",
    }
    (folder / "client.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def prepare_submission_inbox(
    *,
    project_root: Path,
    rec: ClientRecord,
    quarter: str,
    user_id: int,
    archive_existing: bool = True,
) -> Submission:
    """Create a clean inbox folder for a client/quarter.

    Existing customer uploads are moved to inbox/<client>/_archive/ instead of
    being deleted.
    """
    qkey = quarter_key(quarter)
    client_inbox_root = project_root / "inbox" / rec.client_id
    inbox = client_inbox_root / qkey

    if archive_existing and _has_customer_files(inbox):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_dir = client_inbox_root / "_archive" / f"{qkey}_{ts}"
        archive_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(inbox), str(archive_dir))

    inbox.mkdir(parents=True, exist_ok=True)
    _write_inbox_client_json(inbox, rec, user_id)
    out_dir = resolve_output_dir(project_root, qkey, rec.client_id)
    return Submission(
        client_id=rec.client_id,
        client_name=rec.name,
        quarter=qkey,
        inbox=inbox,
        out_dir=out_dir,
    )


def open_existing_submission(
    *,
    project_root: Path,
    rec: ClientRecord,
    quarter: str,
) -> Submission:
    qkey = quarter_key(quarter)
    inbox = project_root / "inbox" / rec.client_id / qkey
    out_dir = resolve_output_dir(project_root, qkey, rec.client_id)
    return Submission(
        client_id=rec.client_id,
        client_name=rec.name,
        quarter=qkey,
        inbox=inbox,
        out_dir=out_dir,
    )


def _slug_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _client_markers(rec: ClientRecord) -> set[str]:
    values = [rec.client_id, rec.name, *rec.aliases]
    return {marker for value in values if (marker := _slug_text(value)) and len(marker) >= 4}


def _load_profile(project_root: Path, client_id: str) -> dict[str, Any] | None:
    rec = load_registry(project_root).get(normalize_client_id(client_id, project_root))
    if rec is None:
        return None
    profile_path = rec.resolve_path("profile_path")
    if profile_path is None or not profile_path.exists():
        return None
    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _known_truck_ids(profile: dict[str, Any] | None) -> set[str]:
    if not profile:
        return set()

    trucks: set[str] = set()
    fleet_evolution = profile.get("fleet_evolution")
    if isinstance(fleet_evolution, dict):
        ever_seen = fleet_evolution.get("trucks_ever_seen")
        if isinstance(ever_seen, list):
            trucks.update(str(x).strip() for x in ever_seen if str(x).strip())

    fleet = profile.get("fleet")
    if isinstance(fleet, dict):
        truck_id = fleet.get("truck_id")
        if truck_id is not None and str(truck_id).strip():
            trucks.add(str(truck_id).strip())
        truck_ids = fleet.get("truck_ids")
        if isinstance(truck_ids, list):
            trucks.update(str(x).strip() for x in truck_ids if str(x).strip())
    return trucks


def check_client_identity(
    *,
    project_root: Path,
    submission: Submission,
    report: PreflightReport,
) -> ClientIdentityReport:
    """Block obvious customer/data-routing mistakes before agent review.

    The LLM can still write nuanced context, but this cheap deterministic check
    catches the dangerous cases: files named for another registered client or
    truck IDs that have no overlap with the selected client's known fleet.
    """
    identity = ClientIdentityReport()
    registry = load_registry(project_root)
    current = registry.get(normalize_client_id(submission.client_id, project_root))

    file_markers = {_slug_text(file.name) for file in report.files}
    for other in registry.values():
        if other.client_id == submission.client_id:
            continue
        matched = sorted(
            marker
            for marker in _client_markers(other)
            if any(marker in file_marker for file_marker in file_markers)
        )
        if matched:
            identity.errors.append(
                f"Uploaded filename appears to reference another client "
                f"({other.client_id}: {other.name}); matched marker(s): {', '.join(matched)}."
            )

    profile = _load_profile(project_root, submission.client_id)
    known_trucks = _known_truck_ids(profile)
    parsed_trucks = {
        truck
        for truck in {*report.trucks_in_miles, *report.trucks_in_fuel}
        if truck and truck != "unknown"
    }
    if known_trucks and parsed_trucks and known_trucks.isdisjoint(parsed_trucks):
        identity.errors.append(
            f"Parsed truck IDs ({', '.join(sorted(parsed_trucks))}) have no overlap with "
            f"{submission.client_name}'s known truck IDs ({', '.join(sorted(known_trucks))})."
        )
    elif known_trucks and parsed_trucks:
        unknown_trucks = parsed_trucks - known_trucks
        if unknown_trucks:
            identity.warnings.append(
                f"New truck IDs not in {submission.client_name}'s profile: "
                f"{', '.join(sorted(unknown_trucks))}. Confirm fleet roster before filing."
            )

    if current is None:
        identity.warnings.append(
            f"Selected client {submission.client_id!r} is not in the registry."
        )
    return identity


def summarize_preflight(
    report: PreflightReport,
    identity: ClientIdentityReport | None = None,
) -> str:
    lines = [
        f"Files: {len(report.files)}",
        f"Parsed mileage rows: {report.mile_rows}",
        f"Parsed fuel rows: {report.fuel_rows}",
    ]
    if report.trucks_in_miles or report.trucks_in_fuel:
        lines.append(
            "Trucks: "
            + ", ".join(sorted(set(report.trucks_in_miles) | set(report.trucks_in_fuel)))
        )
    if report.findings:
        lines.append("")
        lines.append(format_preflight(report))
    else:
        lines.append("Preflight clean.")
    if identity and (identity.errors or identity.warnings):
        lines.append("")
        lines.append(identity.format())
    return "\n".join(lines)


def _finding_warnings(findings: list[Finding]) -> list[str]:
    if not findings:
        return []
    return [format_findings(findings)]


def _fallback_review_note(
    *,
    summary: str,
    findings: list[Finding],
    agent_error: str | None,
) -> ReviewNote:
    issues: list[str | dict[str, Any]] = []
    if findings:
        issues.append(format_findings(findings))
    if agent_error:
        issues.append(f"Agent review failed: {agent_error}")
    if not issues:
        issues.append("No deterministic validator findings.")
    return ReviewNote(
        summary=summary,
        issues=issues,
        filing_reminders=[
            "Review the portal CSV and the Excel review workbook before submitting.",
            "Do not submit if the rate matrix fallback warning appears.",
        ],
        next_steps=[
            "Open ifta_review.xlsx and confirm totals.",
            "Upload ifta_portal.csv only after human review.",
        ],
    )


def _review_note_payload(note: ReviewNote) -> dict[str, object]:
    return {
        "summary": note.summary,
        "issues": note.issues,
        "filing_reminders": note.filing_reminders,
        "next_steps": note.next_steps,
    }


def _metrics_payload(metrics: AgentMetrics | None) -> dict[str, object] | None:
    return asdict(metrics) if metrics else None


def run_delivery(submission: Submission, config: BotConfig) -> DeliveryResult:
    """Run deterministic compute + review and write all customer deliverables."""
    report = preflight_inputs(submission.inbox)
    identity = check_client_identity(
        project_root=config.project_root,
        submission=submission,
        report=report,
    )
    if report.has_errors:
        parts = [format_preflight(report)]
        if identity.errors or identity.warnings:
            parts.append(identity.format())
        raise DeliveryBlockedError("\n\n".join(parts))
    if identity.has_errors:
        raise DeliveryBlockedError(identity.format())

    client_context = load_client_context(
        config.project_root,
        submission.quarter,
        client=submission.client_id,
        inbox=submission.inbox,
    )
    data = ingest_folder(submission.inbox)
    rates_table = fetch_rates(submission.quarter)
    ret = compute_return(data, rates_table)
    findings = validate(data, ret)

    out_dir = resolve_output_dir(config.project_root, submission.quarter, submission.client_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    portal_csv = write_portal_csv(
        ret,
        out_dir / "ifta_portal.csv",
        portal=client_context.portal or "generic",
    )
    write_cleaned_csvs(data, out_dir)

    agent_error: str | None = None
    metrics: AgentMetrics | None = None
    fallback_summary = (
        f"{client_context.client_name} {submission.quarter}: total tax due "
        f"${ret.total_tax_due:,.2f}, fleet MPG {ret.fleet_mpg:.2f}, "
        f"{ret.fleet_miles:,.0f} miles and {ret.fleet_gallons:,.2f} gallons."
    )
    if config.skip_agent:
        note = _fallback_review_note(
            summary=fallback_summary + " Agent review was skipped by bot config.",
            findings=findings,
            agent_error=None,
        )
    else:
        try:
            note, metrics = agent_review(
                submission.quarter,
                client=submission.client_id,
                model=config.agent_model,
                max_tokens=config.agent_max_tokens,
                effort=config.agent_effort,
            )
        except Exception as e:
            agent_error = str(e)
            note = _fallback_review_note(
                summary=fallback_summary + " Agent review failed; use deterministic checks only.",
                findings=findings,
                agent_error=agent_error,
            )

    review_note = write_review_md(note, out_dir / "review_note.md", metrics=metrics)
    review_xlsx = write_owner_review_xlsx(
        ret,
        data,
        out_dir / "ifta_review.xlsx",
        client_name=client_context.client_name,
        review=_review_note_payload(note),
        metrics=_metrics_payload(metrics),
    )

    per_truck_lines = compute_per_truck_lines(data, ret, rates_table)
    truck_files = write_per_truck_filings(
        per_truck_lines,
        fleet_mpg=ret.fleet_mpg,
        quarter=ret.quarter,
        client_name=client_context.client_name,
        fuel=ret.fuel,
        out_dir=out_dir / "trucks",
        data=data,
    )

    warnings = []
    if ret.rate_warning:
        warnings.append(ret.rate_warning)
    warnings.extend(_finding_warnings(findings))
    if agent_error:
        warnings.append(f"Agent review failed: {agent_error}")

    ready_to_file = (
        not ret.rate_fallback_used
        and not agent_error
        and not any(f.severity == "error" for f in findings)
    )
    return DeliveryResult(
        client_name=client_context.client_name,
        quarter=submission.quarter,
        total_tax_due=ret.total_tax_due,
        fleet_miles=ret.fleet_miles,
        fleet_gallons=ret.fleet_gallons,
        fleet_mpg=ret.fleet_mpg,
        ready_to_file=ready_to_file,
        warnings=warnings,
        portal_csv=portal_csv,
        review_xlsx=review_xlsx,
        review_note=review_note,
        truck_files=truck_files,
        output_dir=out_dir,
    )


def _current_submission(context: ContextTypes.DEFAULT_TYPE) -> Submission | None:
    return Submission.from_user_data(_user_data(context).get("submission"))


def _store_submission(context: ContextTypes.DEFAULT_TYPE, submission: Submission) -> None:
    _user_data(context)["submission"] = submission.to_user_data()


def _user_data(context: ContextTypes.DEFAULT_TYPE) -> dict[str, object]:
    if context.user_data is None:
        raise RuntimeError("Telegram user_data is unavailable.")
    return cast(dict[str, object], context.user_data)


def _effective_user_id(update: Update) -> int | None:
    return update.effective_user.id if update.effective_user else None


def _private_chat_error(update: Update) -> str | None:
    chat = update.effective_chat
    if chat is not None and chat.type != "private":
        return "For customer data protection, use this bot only in a private chat."
    return None


async def _reply_private_chat_error(update: Update) -> bool:
    if update.message is None:
        return True
    error = _private_chat_error(update)
    if error is None:
        return False
    await update.message.reply_text(error)
    return True


def _telegram_user_label(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "Unknown Telegram user"
    parts = [part for part in [user.first_name, user.last_name] if part]
    name = " ".join(parts) or "Unknown name"
    username = f"@{user.username}" if user.username else "no username"
    return f"{name} ({username})"


async def _notify_admins(
    context: ContextTypes.DEFAULT_TYPE,
    config: BotConfig,
    text: str,
) -> int:
    delivered = 0
    for admin_id in config.admin_user_ids:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except Exception:
            continue
        delivered += 1
    return delivered


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    user_id = _effective_user_id(update)
    if update.message is None:
        return
    if await _reply_private_chat_error(update):
        return
    if user_id is None:
        await update.message.reply_text("I cannot read your Telegram user id from this chat.")
        return
    await update.message.reply_text(f"Your Telegram user id is: {user_id}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: BotConfig = context.application.bot_data["config"]
    user_id = _effective_user_id(update)
    if update.message is None or user_id is None:
        return
    if await _reply_private_chat_error(update):
        return
    assigned = clients_for_telegram_user(config.project_root, user_id)
    is_admin = user_id in config.admin_user_ids
    if not assigned and not is_admin:
        await update.message.reply_text(
            "Hi. IFTA bot is installed, but your Telegram id is not approved yet.\n"
            f"Your Telegram id is: {user_id}\n\n"
            "To request access, send:\n"
            "/request Your Company Name\n\n"
            "After Eugene approves it, use /new Q2-2026."
        )
        return

    client_line = "Admin access enabled." if is_admin else "Allowed clients: "
    if assigned:
        client_line += ", ".join(f"{rec.client_id} ({rec.name})" for rec in assigned)
    await update.message.reply_text(
        "IFTA intake bot is ready.\n\n"
        f"{client_line}\n\n"
        "Commands:\n"
        "/new Q2-2026 [client] - start a quarter upload\n"
        "/status - check uploaded files\n"
        "/process - run IFTA calculation and review\n"
        "/cancel - cancel current upload session\n"
        "/request [company] - ask Eugene for access\n"
        "/id - show your Telegram user id"
    )


async def request_access_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: BotConfig = context.application.bot_data["config"]
    user_id = _effective_user_id(update)
    if update.message is None or user_id is None:
        return
    if await _reply_private_chat_error(update):
        return

    assigned = clients_for_telegram_user(config.project_root, user_id)
    if assigned:
        await update.message.reply_text(
            "You already have access to: "
            + ", ".join(f"{rec.client_id} ({rec.name})" for rec in assigned)
        )
        return

    requested = " ".join(context.args or []).strip()
    registry = load_registry(config.project_root)
    approve_client = "<client_id>"
    if requested:
        normalized = normalize_client_id(requested, config.project_root)
        if normalized in registry:
            approve_client = normalized

    if not config.admin_user_ids:
        await update.message.reply_text(
            "Access request cannot be sent because no admin Telegram IDs are configured.\n"
            f"Send this id to Eugene: {user_id}"
        )
        return

    admin_text = (
        "IFTA bot access request\n\n"
        f"User: {_telegram_user_label(update)}\n"
        f"Telegram id: {user_id}\n"
        f"Requested company/client: {requested or '(not provided)'}\n\n"
        "Approve from this bot chat with:\n"
        f"/approve {user_id} {approve_client}"
    )
    delivered = await _notify_admins(context, config, admin_text)
    if delivered:
        await update.message.reply_text(
            "Access request sent to Eugene. "
            f"Your Telegram id is {user_id}."
        )
    else:
        await update.message.reply_text(
            "I could not notify Eugene automatically. "
            f"Send him this Telegram id: {user_id}"
        )


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: BotConfig = context.application.bot_data["config"]
    admin_id = _effective_user_id(update)
    if update.message is None or admin_id is None:
        return
    if await _reply_private_chat_error(update):
        return
    if admin_id not in config.admin_user_ids:
        await update.message.reply_text("Admin only.")
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage: /approve <telegram_user_id> <client_id>")
        return
    try:
        customer_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Telegram user id must be digits.")
        return
    requested_client = " ".join(args[1:]).strip()

    try:
        rec = approve_telegram_user(
            project_root=config.project_root,
            user_id=customer_id,
            requested_client=requested_client,
        )
    except AuthorizationError as e:
        await update.message.reply_text(str(e))
        return

    await update.message.reply_text(
        f"Approved Telegram id {customer_id} for {rec.name} ({rec.client_id})."
    )
    try:
        await context.bot.send_message(
            chat_id=customer_id,
            text=(
                f"Access approved for {rec.name}.\n\n"
                "Start your quarter upload with:\n"
                "/new Q2-2026"
            ),
        )
    except Exception:
        await update.message.reply_text(
            "Approval saved. I could not notify the customer automatically, "
            "but they can send /start now."
        )


async def clients_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: BotConfig = context.application.bot_data["config"]
    user_id = _effective_user_id(update)
    if update.message is None or user_id is None:
        return
    if await _reply_private_chat_error(update):
        return
    if user_id in config.admin_user_ids:
        records = list(load_registry(config.project_root).values())
    else:
        records = clients_for_telegram_user(config.project_root, user_id)
    if not records:
        await update.message.reply_text("No clients are attached to your Telegram id. Send /id.")
        return
    await update.message.reply_text(
        "Clients:\n"
        + "\n".join(f"- {rec.client_id}: {rec.name} ({rec.portal})" for rec in records)
    )


def _split_quarter_and_client(args: list[str] | None) -> tuple[str | None, str | None]:
    if not args:
        return None, None
    quarter = args[0]
    client = " ".join(args[1:]).strip() or None
    return quarter, client


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: BotConfig = context.application.bot_data["config"]
    user_id = _effective_user_id(update)
    if update.message is None or user_id is None:
        return
    if await _reply_private_chat_error(update):
        return
    quarter, requested_client = _split_quarter_and_client(context.args)
    if not quarter:
        await update.message.reply_text("Usage: /new Q2-2026 [client_id]")
        return
    try:
        qkey = quarter_key(quarter)
        rec = resolve_authorized_client(
            project_root=config.project_root,
            user_id=user_id,
            requested_client=requested_client,
            admin_user_ids=config.admin_user_ids,
        )
        submission = prepare_submission_inbox(
            project_root=config.project_root,
            rec=rec,
            quarter=qkey,
            user_id=user_id,
        )
    except (ValueError, AuthorizationError) as e:
        await update.message.reply_text(str(e))
        return

    _store_submission(context, submission)
    await update.message.reply_text(
        f"Started {submission.quarter} for {submission.client_name}.\n"
        "Upload mileage and fuel-card files as documents: CSV, Excel, or PDF.\n"
        "When both parse cleanly, send /process."
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    if await _reply_private_chat_error(update):
        return
    _user_data(context).pop("submission", None)
    await update.message.reply_text("Current upload session cancelled. Saved files were not deleted.")


def _submission_from_process_args(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    config: BotConfig,
) -> Submission | None:
    user_id = _effective_user_id(update)
    if user_id is None:
        return None
    quarter, requested_client = _split_quarter_and_client(context.args)
    if quarter is None:
        submission = _current_submission(context)
        if submission is not None:
            authorize_submission_access(
                project_root=config.project_root,
                user_id=user_id,
                submission=submission,
                admin_user_ids=config.admin_user_ids,
            )
        return submission
    rec = resolve_authorized_client(
        project_root=config.project_root,
        user_id=user_id,
        requested_client=requested_client,
        admin_user_ids=config.admin_user_ids,
    )
    return open_existing_submission(project_root=config.project_root, rec=rec, quarter=quarter)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: BotConfig = context.application.bot_data["config"]
    if update.message is None:
        return
    if await _reply_private_chat_error(update):
        return
    try:
        submission = _submission_from_process_args(update, context, config)
    except (ValueError, AuthorizationError) as e:
        await update.message.reply_text(str(e))
        return
    if submission is None:
        await update.message.reply_text("No active submission. Start with /new Q2-2026.")
        return
    report = preflight_inputs(submission.inbox)
    identity = check_client_identity(
        project_root=config.project_root,
        submission=submission,
        report=report,
    )
    await update.message.reply_text(
        f"{submission.client_name} {submission.quarter}\n\n{summarize_preflight(report, identity)}"
    )


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: BotConfig = context.application.bot_data["config"]
    user_id = _effective_user_id(update)
    if update.message is None or update.message.document is None or user_id is None:
        return
    if await _reply_private_chat_error(update):
        return
    submission = _current_submission(context)
    if submission is None:
        await update.message.reply_text("Start an upload first: /new Q2-2026 [client_id]")
        return
    try:
        authorize_submission_access(
            project_root=config.project_root,
            user_id=user_id,
            submission=submission,
            admin_user_ids=config.admin_user_ids,
        )
    except AuthorizationError as e:
        _user_data(context).pop("submission", None)
        await update.message.reply_text(str(e))
        return

    document = update.message.document
    filename = safe_filename(document.file_name or document.file_unique_id)
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_UPLOAD_SUFFIXES:
        await update.message.reply_text(
            f"Unsupported file type {suffix or '(none)'}. Send CSV, Excel, or PDF."
        )
        return
    if document.file_size and document.file_size > config.max_file_mb * 1024 * 1024:
        await update.message.reply_text(
            f"File is too large. Limit is {config.max_file_mb:g} MB."
        )
        return

    await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    destination = unique_destination(submission.inbox, filename)
    tg_file = await document.get_file()
    await tg_file.download_to_drive(custom_path=str(destination))
    submission.uploaded_files.append(destination.name)
    _store_submission(context, submission)

    report = preflight_inputs(submission.inbox)
    identity = check_client_identity(
        project_root=config.project_root,
        submission=submission,
        report=report,
    )
    if not report.has_errors and not identity.has_errors and report.mile_rows > 0 and report.fuel_rows > 0:
        next_line = "\nReady to process. Send /process."
    elif identity.has_errors:
        next_line = "\nDo not process this upload. Use /cancel, then start the correct client/quarter."
    else:
        next_line = "\nUpload the missing file(s), then send /status."
    await update.message.reply_text(
        f"Saved {destination.name}.\n\n{summarize_preflight(report, identity)}{next_line}"
    )


async def process_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: BotConfig = context.application.bot_data["config"]
    if update.message is None:
        return
    if await _reply_private_chat_error(update):
        return
    try:
        submission = _submission_from_process_args(update, context, config)
    except (ValueError, AuthorizationError) as e:
        await update.message.reply_text(str(e))
        return
    if submission is None:
        await update.message.reply_text("No active submission. Start with /new Q2-2026.")
        return
    if not submission.inbox.exists():
        await update.message.reply_text(f"Inbox not found: {submission.inbox}")
        return

    await update.message.reply_text(
        f"Processing {submission.client_name} {submission.quarter}. This can take a few minutes."
    )
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        result = await asyncio.to_thread(run_delivery, submission, config)
    except DeliveryBlockedError as e:
        await update.message.reply_text(f"Processing blocked this submission:\n\n{e}")
        return
    except Exception as e:
        await update.message.reply_text(f"Processing failed: {type(e).__name__}: {e}")
        return

    status = "READY FOR HUMAN REVIEW" if result.ready_to_file else "REVIEW REQUIRED"
    warning_text = "\n\nWarnings:\n" + "\n".join(f"- {w}" for w in result.warnings) if result.warnings else ""
    await update.message.reply_text(
        f"{status}\n"
        f"{result.client_name} {result.quarter}\n"
        f"Fleet miles: {result.fleet_miles:,.0f}\n"
        f"Fleet gallons: {result.fleet_gallons:,.2f}\n"
        f"Fleet MPG: {result.fleet_mpg:.2f}\n"
        f"Total tax due: ${result.total_tax_due:,.2f}"
        f"{warning_text}\n\n"
        "I am sending the customer packet now. Review before submitting to the portal."
    )

    for path in result.customer_files:
        if not path.exists():
            continue
        await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
        with path.open("rb") as fh:
            await update.message.reply_document(document=fh, filename=path.name)


def build_application(config: BotConfig) -> Application:
    app = ApplicationBuilder().token(config.token).build()
    app.bot_data["config"] = config
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("request", request_access_command))
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("clients", clients_command))
    app.add_handler(CommandHandler("new", new_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("process", process_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    return app


def run_polling(config: BotConfig) -> None:
    app = build_application(config)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
