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
import contextlib
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
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
from ifta.notify import AdminNotifier, format_event, load_admin_notifier_config
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


def _read_raw_access_file(project_root: Path) -> dict[str, Any]:
    """Read the full access file as a dict so we can preserve unknown sections."""
    path = telegram_access_path(project_root)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_raw_access_file(project_root: Path, payload: dict[str, Any]) -> Path:
    path = telegram_access_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_telegram_access(project_root: Path, access: dict[str, set[int]]) -> Path:
    """Persist client approvals, preserving any other top-level sections."""
    raw = _read_raw_access_file(project_root)
    raw["clients"] = {
        client_id: sorted(ids)
        for client_id, ids in sorted(access.items())
        if ids
    }
    return _write_raw_access_file(project_root, raw)


# ─── Pending users (unapproved DMs we've seen) ────────────────────────────


@dataclass(frozen=True)
class PendingUser:
    user_id: int
    username: str | None
    name: str | None
    first_seen: str  # ISO timestamp


def load_pending_users(project_root: Path) -> list[PendingUser]:
    raw = _read_raw_access_file(project_root)
    items = raw.get("pending", [])
    if not isinstance(items, list):
        return []
    out: list[PendingUser] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        try:
            uid = int(entry["user_id"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append(
            PendingUser(
                user_id=uid,
                username=entry.get("username") or None,
                name=entry.get("name") or None,
                first_seen=str(entry.get("first_seen") or ""),
            )
        )
    return out


def add_pending_user(
    project_root: Path,
    *,
    user_id: int,
    username: str | None,
    name: str | None,
) -> None:
    """Record an unapproved DM. Idempotent — duplicates are merged in-place."""
    raw = _read_raw_access_file(project_root)
    items = raw.get("pending")
    if not isinstance(items, list):
        items = []
    # Drop any existing entry for this user_id so we keep newest username/name.
    items = [e for e in items if not (isinstance(e, dict) and e.get("user_id") == user_id)]
    items.append(
        {
            "user_id": user_id,
            "username": username,
            "name": name,
            "first_seen": datetime.now().isoformat(timespec="seconds"),
        }
    )
    raw["pending"] = items
    _write_raw_access_file(project_root, raw)


def remove_pending_user(project_root: Path, user_id: int) -> None:
    raw = _read_raw_access_file(project_root)
    items = raw.get("pending")
    if not isinstance(items, list):
        return
    raw["pending"] = [
        e for e in items if not (isinstance(e, dict) and e.get("user_id") == user_id)
    ]
    _write_raw_access_file(project_root, raw)


# ─── Preauth (admin pre-approves a future user by @username) ──────────────


def _normalize_username(raw: str | None) -> str | None:
    if not raw:
        return None
    return raw.strip().lstrip("@").strip().lower() or None


def load_preauth(project_root: Path) -> dict[str, str]:
    """Return {lowercased_username: client_id}."""
    raw = _read_raw_access_file(project_root)
    items = raw.get("preauth", {})
    if not isinstance(items, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in items.items():
        norm = _normalize_username(str(k))
        if norm and isinstance(v, str) and v.strip():
            out[norm] = v.strip()
    return out


def set_preauth(project_root: Path, username: str, client_id: str) -> str:
    """Record a username→client preauth. Returns the normalized username."""
    norm = _normalize_username(username)
    if not norm:
        raise ValueError("Username is required (e.g. @some_user).")
    raw = _read_raw_access_file(project_root)
    items = raw.get("preauth")
    if not isinstance(items, dict):
        items = {}
    items[norm] = client_id
    raw["preauth"] = items
    _write_raw_access_file(project_root, raw)
    return norm


def remove_preauth(project_root: Path, username: str) -> bool:
    norm = _normalize_username(username)
    if not norm:
        return False
    raw = _read_raw_access_file(project_root)
    items = raw.get("preauth")
    if not isinstance(items, dict) or norm not in items:
        return False
    del items[norm]
    raw["preauth"] = items
    _write_raw_access_file(project_root, raw)
    return True


# ─── Phone-based preauth (for contacts shared without a Telegram id) ─────


def normalize_phone(raw: str | None) -> str | None:
    """Strip everything but digits; prefix with '+'. None on empty input.

    "+1 (916) 936-5441"  →  "+19169365441"
    "19169365441"        →  "+19169365441"
    "+1 916 936 5441"    →  "+19169365441"
    """
    if not raw:
        return None
    digits = "".join(c for c in raw if c.isdigit())
    return f"+{digits}" if digits else None


def load_phone_preauth(project_root: Path) -> dict[str, dict[str, str | None]]:
    """{normalized_phone: {client_id, name, added_at}}."""
    raw = _read_raw_access_file(project_root)
    items = raw.get("preauth_by_phone")
    if not isinstance(items, dict):
        return {}
    out: dict[str, dict[str, str | None]] = {}
    for phone, entry in items.items():
        if not isinstance(entry, dict):
            continue
        norm = normalize_phone(str(phone))
        client_id = entry.get("client_id")
        if norm and isinstance(client_id, str) and client_id:
            out[norm] = {
                "client_id": client_id,
                "name": entry.get("name") or None,
                "added_at": entry.get("added_at") or None,
            }
    return out


def set_phone_preauth(
    project_root: Path,
    phone: str,
    client_id: str,
    *,
    name: str | None = None,
) -> str:
    """Record a phone→client preauth. Returns the normalized phone."""
    norm = normalize_phone(phone)
    if not norm:
        raise ValueError("Phone number is required.")
    raw = _read_raw_access_file(project_root)
    items = raw.get("preauth_by_phone")
    if not isinstance(items, dict):
        items = {}
    items[norm] = {
        "client_id": client_id,
        "name": name,
        "added_at": datetime.now().isoformat(timespec="seconds"),
    }
    raw["preauth_by_phone"] = items
    _write_raw_access_file(project_root, raw)
    return norm


def get_phone_preauth(
    project_root: Path, phone: str
) -> dict[str, str | None] | None:
    norm = normalize_phone(phone)
    if not norm:
        return None
    return load_phone_preauth(project_root).get(norm)


def remove_phone_preauth(project_root: Path, phone: str) -> bool:
    norm = normalize_phone(phone)
    if not norm:
        return False
    raw = _read_raw_access_file(project_root)
    items = raw.get("preauth_by_phone")
    if not isinstance(items, dict) or norm not in items:
        return False
    del items[norm]
    raw["preauth_by_phone"] = items
    _write_raw_access_file(project_root, raw)
    return True


# ─── Known users (anyone who has DMed the bot, ever) ──────────────────────


@dataclass(frozen=True)
class KnownUser:
    user_id: int
    username: str | None
    name: str | None
    last_seen: str


def upsert_known_user(
    project_root: Path,
    *,
    user_id: int,
    username: str | None,
    name: str | None,
) -> None:
    """Record/refresh a user's @username + name. Called from every DM."""
    raw = _read_raw_access_file(project_root)
    items = raw.get("known_users")
    if not isinstance(items, dict):
        items = {}
    items[str(user_id)] = {
        "username": username or None,
        "name": name or None,
        "last_seen": datetime.now().isoformat(timespec="seconds"),
    }
    raw["known_users"] = items
    _write_raw_access_file(project_root, raw)


def get_known_user(project_root: Path, user_id: int) -> KnownUser | None:
    raw = _read_raw_access_file(project_root)
    items = raw.get("known_users")
    if not isinstance(items, dict):
        return None
    entry = items.get(str(user_id))
    if not isinstance(entry, dict):
        return None
    return KnownUser(
        user_id=user_id,
        username=entry.get("username") or None,
        name=entry.get("name") or None,
        last_seen=str(entry.get("last_seen") or ""),
    )


def format_user_label(known: KnownUser | None, user_id: int) -> str:
    """Human-readable label for inline buttons + listings."""
    if known is None:
        return f"id={user_id}"
    if known.username and known.name:
        return f"@{known.username} ({known.name})"
    if known.username:
        return f"@{known.username}"
    if known.name:
        return f"{known.name} (id={user_id})"
    return f"id={user_id}"


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


def revoke_telegram_user(project_root: Path, user_id: int) -> list[str]:
    """Remove `user_id` from every client's runtime allowlist.

    Returns the list of client_ids that previously contained this user (useful
    so the caller can tell the admin which carriers were affected). An empty
    list means the user wasn't on any local allowlist — they might still be in
    a client.json `telegram_user_ids` field, which is admin-edited by hand.
    """
    access = load_telegram_access(project_root)
    removed: list[str] = []
    for client_id, ids in access.items():
        if user_id in ids:
            ids.discard(user_id)
            removed.append(client_id)
    write_telegram_access(project_root, access)
    return removed


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
            f"Admin account: pass a client id, e.g. /new {current_filing_quarter()} dm_express."
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


def current_filing_quarter(today: datetime | None = None) -> str:
    """Quarter most customers are filing for *now*.

    Filing happens in the month after a quarter closes: in May you file Q1,
    in Feb you file Q4 of the prior year. Returning the just-completed
    quarter means the /new keyboard button is right ~99% of the time.
    """
    now = today or datetime.now()
    # 1-indexed calendar quarter of the current date.
    cal_q = (now.month - 1) // 3 + 1
    year = now.year
    filing_q = cal_q - 1
    if filing_q == 0:
        filing_q = 4
        year -= 1
    return f"Q{filing_q}-{year}"


def _approved_keyboard(quarter: str | None = None) -> ReplyKeyboardMarkup:
    """Reply keyboard for an approved customer: 5 buttons in 3 rows."""
    qkey = quarter or current_filing_quarter()
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(f"/new {qkey}"), KeyboardButton("/status")],
            [KeyboardButton("/process"), KeyboardButton("/cancel")],
            [KeyboardButton("/help")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _admin_keyboard(
    project_root: Path, *, quarter: str | None = None
) -> ReplyKeyboardRemove:
    """Admin sees no reply keyboard at all.

    The IFTA bot is event-driven for admins: the primary action ('add a
    customer') is via paperclip → Contact, and pending users are surfaced via
    proactive DMs with inline [Approve]/[Ignore] buttons. Typed commands
    (/pending, /clients, /preauth, /unapprove, /onboard) still work but
    don't clutter the chat. `project_root` + `quarter` kept for caller
    compatibility but are intentionally unused.
    """
    del project_root, quarter
    return ReplyKeyboardRemove()


def _unapproved_keyboard(
    project_root: Path | None = None,
) -> ReplyKeyboardMarkup:
    """Minimal keyboard for users not yet approved.

    If `project_root` is supplied and any phone preauths exist, also offer a
    'Share my contact' button — Telegram sends the user's contact info when
    they tap it, which the bot then matches against `preauth_by_phone`.
    """
    rows: list[list[KeyboardButton]] = [[KeyboardButton("/id")]]
    if project_root is not None and load_phone_preauth(project_root):
        rows.insert(
            0,
            [KeyboardButton("📱 Share my contact", request_contact=True)],
        )
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def _keyboard_for_user(
    *, project_root: Path, user_id: int | None, admin_user_ids: tuple[int, ...]
) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    """Pick the right keyboard based on the caller's auth status."""
    if user_id is None:
        return _unapproved_keyboard(project_root)
    if user_id in admin_user_ids:
        return _admin_keyboard(project_root)
    if clients_for_telegram_user(project_root, user_id):
        return _approved_keyboard()
    return _unapproved_keyboard(project_root)


def _keyboard_from_context(
    update: Update, config: BotConfig
) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    """Shortcut wrapper that pulls user id from the Update."""
    return _keyboard_for_user(
        project_root=config.project_root,
        user_id=_effective_user_id(update),
        admin_user_ids=config.admin_user_ids,
    )


async def _notify_admins_of_new_pending_user(
    context: ContextTypes.DEFAULT_TYPE,
    config: BotConfig,
    *,
    user_id: int,
    username: str | None,
    name: str | None,
) -> None:
    """DM every admin when a brand-new user lands in /pending.

    Inline buttons let the admin act without ever opening /pending — tap
    [Approve] → carrier picker → done, or [Ignore] to dismiss. Reuses the
    existing CB_PENDING_PICK_USER callback so all approval logic stays in
    one place.
    """
    handle = f"@{username}" if username else "(no @username)"
    display = name or "(no name)"
    text = (
        "🆕 New user wants access to the IFTA bot:\n\n"
        f"{handle} — {display}\n"
        f"Telegram id: {user_id}"
    )
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✓ Approve",
                    callback_data=f"{CB_PENDING_PICK_USER}:{user_id}",
                ),
                InlineKeyboardButton(
                    "✕ Ignore",
                    callback_data=f"{CB_PENDING_IGNORE}:{user_id}",
                ),
            ]
        ]
    )
    for admin_id in config.admin_user_ids:
        with contextlib.suppress(Exception):
            await context.bot.send_message(
                chat_id=admin_id, text=text, reply_markup=markup
            )


async def _first_contact_middleware(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """On every incoming message, either auto-approve via preauth or log to pending.

    Registered at handler-group=-1 so it runs before all command handlers. It
    never blocks or transforms the actual update — it just maintains the
    pending/preauth lists in `data/telegram_access.json` and DMs the user when
    they're auto-approved.
    """
    config: BotConfig = context.application.bot_data["config"]
    user = update.effective_user
    if user is None:
        return
    user_id = user.id

    # Refresh known_users for everyone (admins included — useful if Eugene
    # ever needs to unapprove himself or check his own label).
    with contextlib.suppress(Exception):
        upsert_known_user(
            config.project_root,
            user_id=user_id,
            username=user.username,
            name=user.full_name or None,
        )

    if user_id in config.admin_user_ids:
        return
    if clients_for_telegram_user(config.project_root, user_id):
        return

    # Check preauth by @username first — if matched, approve and notify.
    norm_username = _normalize_username(user.username)
    preauth = load_preauth(config.project_root)
    target_client = preauth.get(norm_username) if norm_username else None
    if target_client:
        try:
            rec = approve_telegram_user(
                project_root=config.project_root,
                user_id=user_id,
                requested_client=target_client,
            )
        except (AuthorizationError, ValueError):
            return  # bad preauth entry; leave it alone
        remove_preauth(config.project_root, norm_username or "")
        remove_pending_user(config.project_root, user_id)
        with contextlib.suppress(Exception):
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"Access approved for {rec.name}.\n\n"
                    "Start your quarter upload with:\n"
                    f"/new {current_filing_quarter()}"
                ),
                reply_markup=_approved_keyboard(),
            )
        return

    # Otherwise: record as pending so admin can see them with /pending, and
    # proactively DM admins the first time we see this user (subsequent
    # messages from the same user won't re-ping).
    already_pending = any(
        p.user_id == user_id for p in load_pending_users(config.project_root)
    )
    # Failing to log a pending user should never crash a handler.
    with contextlib.suppress(Exception):
        add_pending_user(
            config.project_root,
            user_id=user_id,
            username=user.username,
            name=user.full_name or None,
        )
    if not already_pending:
        await _notify_admins_of_new_pending_user(
            context,
            config,
            user_id=user_id,
            username=user.username,
            name=user.full_name or None,
        )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: BotConfig = context.application.bot_data["config"]
    user_id = _effective_user_id(update)
    if update.message is None:
        return
    if await _reply_private_chat_error(update):
        return
    if user_id is None:
        await update.message.reply_text("I cannot read your Telegram user id from this chat.")
        return
    await update.message.reply_text(
        f"Your Telegram user id is: {user_id}",
        reply_markup=_keyboard_from_context(update, config),
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: BotConfig = context.application.bot_data["config"]
    user_id = _effective_user_id(update)
    if update.message is None or user_id is None:
        return
    if await _reply_private_chat_error(update):
        return
    assigned = clients_for_telegram_user(config.project_root, user_id)
    is_admin = user_id in config.admin_user_ids
    quarter_hint = current_filing_quarter()
    if not assigned and not is_admin:
        has_phone_preauth = bool(load_phone_preauth(config.project_root))
        verify_line = (
            "\nIf Eugene already added you by phone, tap "
            "'📱 Share my contact' to auto-approve."
            if has_phone_preauth
            else ""
        )
        await update.message.reply_text(
            "Hi. IFTA bot is installed, but your Telegram id is not approved yet.\n"
            f"Your Telegram id is: {user_id}\n\n"
            "To request access, send:\n"
            "/request Your Company Name\n\n"
            f"After Eugene approves it, use /new {quarter_hint}." + verify_line,
            reply_markup=_unapproved_keyboard(config.project_root),
        )
        return

    client_line = "Admin access enabled." if is_admin else "Allowed clients: "
    if assigned:
        client_line += ", ".join(f"{rec.client_id} ({rec.name})" for rec in assigned)

    customer_commands = (
        f"/new {quarter_hint} [client] - start a quarter upload\n"
        "/status - check uploaded files\n"
        "/process - run IFTA calculation and review\n"
        "/cancel - cancel current upload session\n"
        "/request [company] - ask Eugene for access\n"
        "/id - show your Telegram user id"
    )
    admin_commands = (
        "\n\nAdding customers — primary path:\n"
        "📎 → Contact → pick customer → Send\n"
        "  then tap the carrier in the picker.\n\n"
        "Admin commands (typed):\n"
        "/pending - users who DMed the bot but aren't approved\n"
        "/clients - registered carriers\n"
        "/onboard <client_id> [Name] [STATE] - add a new carrier\n"
        "/preauth @user <client_id> - pre-approve by username\n"
        "/approve <id> <client_id> - approve by numeric id\n"
        "/unapprove <id> - revoke access (or /unapprove for a picker)"
    )
    body = "IFTA intake bot is ready.\n\n" + client_line + "\n\nCommands:\n" + customer_commands
    if is_admin:
        body += admin_commands
    await update.message.reply_text(
        body,
        reply_markup=_keyboard_from_context(update, config),
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
                f"/new {current_filing_quarter()}"
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


# ─── Callback-query infrastructure ────────────────────────────────────────
# Inline-keyboard taps come back as callback_data strings. Use short prefixes
# because Telegram caps callback_data at 64 bytes total.

CB_PENDING_PICK_USER = "pa-pu"      # pa-pu:<user_id>          — user picked
CB_PENDING_PICK_CLIENT = "pa-pc"    # pa-pc:<user_id>:<client> — client picked → approve
CB_PENDING_IGNORE = "pa-ig"         # pa-ig:<user_id>          — ignore (remove from pending)
CB_PENDING_REFRESH = "pa-rf"        # pa-rf                    — re-render list
CB_PENDING_BACK = "pa-bk"           # pa-bk                    — back to user list
CB_REVOKE_PICK = "un-pk"            # un-pk:<user_id>          — user picked
CB_REVOKE_CONFIRM = "un-ok"         # un-ok:<user_id>          — confirmed
CB_REVOKE_REFRESH = "un-rf"         # un-rf                    — re-render list
CB_REVOKE_BACK = "un-bk"            # un-bk                    — back to user list
CB_CONTACT_APPROVE = "ct-c"         # ct-c:<user_id>:<client>  — approve from shared contact
CB_PHONE_PREAUTH = "pp-c"           # pp-c:<phone-no-plus>:<client> — phone preauth from contact
CB_CANCEL = "x"                     # x                        — done/dismiss

# Web-intake submission approval (sent by the FastAPI app, handled here).
CB_WEB_ACCEPT = "wa"                # wa:<submission_id>       — accept web submission
CB_WEB_DECLINE = "wd"               # wd:<submission_id>       — decline web submission
CB_WEB_MORE_FILES = "wm"            # wm:<submission_id>       — request more files


def _list_pending(project_root: Path) -> list[PendingUser]:
    """Pending users minus any who slipped into approved since being logged."""
    return [
        p for p in load_pending_users(project_root)
        if not clients_for_telegram_user(project_root, p.user_id)
    ]


def _build_pending_user_list_markup(
    items: list[PendingUser],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for p in items:
        label = f"✓ @{p.username}" if p.username else f"✓ id={p.user_id}"
        if p.name:
            label = f"{label} — {p.name}"
        # Truncate to keep callback_data + label combined under render limits.
        rows.append(
            [InlineKeyboardButton(label[:55], callback_data=f"{CB_PENDING_PICK_USER}:{p.user_id}")]
        )
    rows.append(
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=CB_PENDING_REFRESH),
            InlineKeyboardButton("✕ Close", callback_data=CB_CANCEL),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _build_client_picker_markup(
    project_root: Path,
    *,
    callback_prefix: str,
    user_id: int,
) -> InlineKeyboardMarkup:
    """Inline keyboard listing every registered client (one button per row)."""
    registry = load_registry(project_root)
    rows: list[list[InlineKeyboardButton]] = []
    for rec in registry.values():
        rows.append(
            [
                InlineKeyboardButton(
                    f"{rec.name} ({rec.client_id})",
                    callback_data=f"{callback_prefix}:{user_id}:{rec.client_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("← Back", callback_data=CB_PENDING_BACK),
            InlineKeyboardButton("✕ Cancel", callback_data=CB_CANCEL),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: show pending users as tappable inline buttons."""
    config: BotConfig = context.application.bot_data["config"]
    user_id = _effective_user_id(update)
    if update.message is None or user_id is None:
        return
    if await _reply_private_chat_error(update):
        return
    if user_id not in config.admin_user_ids:
        await update.message.reply_text("Admin only.")
        return

    items = _list_pending(config.project_root)
    if not items:
        await update.message.reply_text(
            "No pending users yet.\n\n"
            "Anyone who DMs the bot from now on will appear here. "
            "Or use /preauth @username <client_id> to pre-approve someone "
            "before they DM."
        )
        return
    await update.message.reply_text(
        f"Pending users ({len(items)}). Tap to approve:",
        reply_markup=_build_pending_user_list_markup(items),
    )


async def pending_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all callback-data values that start with the pending-flow prefix."""
    query = update.callback_query
    if query is None or query.data is None:
        return
    config: BotConfig = context.application.bot_data["config"]
    actor_id = update.effective_user.id if update.effective_user else None
    if actor_id is None or actor_id not in config.admin_user_ids:
        await query.answer("Admin only.", show_alert=True)
        return

    data = query.data
    if data == CB_CANCEL:
        await query.answer()
        with contextlib.suppress(Exception):
            await query.edit_message_text("Closed.")
        return

    if data in (CB_PENDING_REFRESH, CB_PENDING_BACK):
        items = _list_pending(config.project_root)
        if not items:
            await query.answer()
            with contextlib.suppress(Exception):
                await query.edit_message_text("No pending users.")
            return
        await query.answer()
        with contextlib.suppress(Exception):
            await query.edit_message_text(
                f"Pending users ({len(items)}). Tap to approve:",
                reply_markup=_build_pending_user_list_markup(items),
            )
        return

    if data.startswith(f"{CB_PENDING_PICK_USER}:"):
        try:
            target = int(data.split(":", 1)[1])
        except (IndexError, ValueError):
            await query.answer("Bad payload.", show_alert=True)
            return
        registry = load_registry(config.project_root)
        if not registry:
            await query.answer()
            with contextlib.suppress(Exception):
                await query.edit_message_text(
                    "No carriers are registered yet. Run /onboard first."
                )
            return
        known = get_known_user(config.project_root, target)
        label = format_user_label(known, target)
        await query.answer()
        with contextlib.suppress(Exception):
            await query.edit_message_text(
                f"Approve {label} for which carrier?",
                reply_markup=_build_client_picker_markup(
                    config.project_root,
                    callback_prefix=CB_PENDING_PICK_CLIENT,
                    user_id=target,
                ),
            )
        return

    if data.startswith(f"{CB_PENDING_IGNORE}:"):
        try:
            target = int(data.split(":", 1)[1])
        except (IndexError, ValueError):
            await query.answer("Bad payload.", show_alert=True)
            return
        known = get_known_user(config.project_root, target)
        label = format_user_label(known, target)
        remove_pending_user(config.project_root, target)
        await query.answer("Ignored.")
        with contextlib.suppress(Exception):
            await query.edit_message_text(
                f"✕ Ignored {label}. They won't appear in /pending again "
                "unless they DM the bot."
            )
        return

    if data.startswith(f"{CB_PENDING_PICK_CLIENT}:"):
        parts = data.split(":", 2)
        if len(parts) != 3:
            await query.answer("Bad payload.", show_alert=True)
            return
        try:
            target = int(parts[1])
        except ValueError:
            await query.answer("Bad payload.", show_alert=True)
            return
        client_id = parts[2]
        try:
            rec = approve_telegram_user(
                project_root=config.project_root,
                user_id=target,
                requested_client=client_id,
            )
        except (AuthorizationError, ValueError) as e:
            await query.answer(str(e), show_alert=True)
            return
        remove_pending_user(config.project_root, target)
        known = get_known_user(config.project_root, target)
        label = format_user_label(known, target)
        customer_first = known.name.split(" ")[0] if known and known.name else None
        _, status, share_markup = await _try_notify_new_customer(
            context,
            chat_id=target,
            client_name=rec.name,
            customer_label=customer_first,
        )
        await query.answer("Approved.")
        with contextlib.suppress(Exception):
            await query.edit_message_text(
                f"✓ Approved {label} for {rec.name} ({rec.client_id}).\n\n{status}",
                reply_markup=share_markup,
            )
        return

    await query.answer("Unknown action.", show_alert=True)


async def preauth_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /preauth @username <client_id> | list | remove @username."""
    config: BotConfig = context.application.bot_data["config"]
    user_id = _effective_user_id(update)
    if update.message is None or user_id is None:
        return
    if await _reply_private_chat_error(update):
        return
    if user_id not in config.admin_user_ids:
        await update.message.reply_text("Admin only.")
        return

    args = context.args or []
    if not args or args[0].lower() == "list":
        items = load_preauth(config.project_root)
        if not items:
            await update.message.reply_text(
                "No preauth entries.\n\n"
                "Usage:\n"
                "  /preauth @user dm_express  - pre-approve a user by username\n"
                "  /preauth list              - show all preauth entries\n"
                "  /preauth remove @user      - drop one"
            )
            return
        body = "\n".join(f"- @{u} → {c}" for u, c in sorted(items.items()))
        await update.message.reply_text(f"Preauth entries:\n{body}")
        return

    if args[0].lower() == "remove":
        if len(args) < 2:
            await update.message.reply_text("Usage: /preauth remove @username")
            return
        if remove_preauth(config.project_root, args[1]):
            await update.message.reply_text(f"Removed preauth for {args[1]}.")
        else:
            await update.message.reply_text(f"No preauth entry for {args[1]}.")
        return

    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /preauth @username <client_id>\n"
            "(They'll be auto-approved as soon as they DM the bot.)"
        )
        return
    username = args[0]
    requested_client = " ".join(args[1:]).strip()
    registry = load_registry(config.project_root)
    client_id = normalize_client_id(requested_client, config.project_root)
    if client_id not in registry:
        await update.message.reply_text(
            f"Unknown client {requested_client!r}. Registered: {', '.join(registry) or '(none)'}"
        )
        return
    try:
        norm = set_preauth(config.project_root, username, client_id)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return
    rec = registry[client_id]
    await update.message.reply_text(
        f"Preauthorized @{norm} for {rec.name} ({rec.client_id}).\n"
        "They'll be approved the moment they DM the bot."
    )


def _all_approved_user_ids(project_root: Path) -> dict[int, list[str]]:
    """Return {user_id: [client_ids that include them]} for the local allowlist."""
    out: dict[int, list[str]] = {}
    for client_id, ids in load_telegram_access(project_root).items():
        for uid in ids:
            out.setdefault(uid, []).append(client_id)
    return out


def _build_revoke_user_list_markup(
    project_root: Path,
    *,
    by_user: dict[int, list[str]],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for uid in sorted(by_user):
        known = get_known_user(project_root, uid)
        label = format_user_label(known, uid)
        clients = ", ".join(by_user[uid])
        full = f"{label} — {clients}"
        rows.append(
            [InlineKeyboardButton(full[:55], callback_data=f"{CB_REVOKE_PICK}:{uid}")]
        )
    rows.append(
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=CB_REVOKE_REFRESH),
            InlineKeyboardButton("✕ Close", callback_data=CB_CANCEL),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def unapprove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: pick a user from inline buttons → confirm → revoke from all clients."""
    config: BotConfig = context.application.bot_data["config"]
    user_id = _effective_user_id(update)
    if update.message is None or user_id is None:
        return
    if await _reply_private_chat_error(update):
        return
    if user_id not in config.admin_user_ids:
        await update.message.reply_text("Admin only.")
        return

    # `/unapprove <id>` typed-mode fast path still works.
    args = context.args or []
    if args:
        try:
            target = int(args[0])
        except ValueError:
            await update.message.reply_text("Telegram user id must be digits.")
            return
        removed = revoke_telegram_user(config.project_root, target)
        if removed:
            await update.message.reply_text(
                f"Revoked access for user {target} from: {', '.join(removed)}."
            )
        else:
            await update.message.reply_text(
                f"User {target} was not on any local access list."
            )
        return

    by_user = _all_approved_user_ids(config.project_root)
    if not by_user:
        await update.message.reply_text(
            "No approved customers on the local access list.\n\n"
            "If someone's in a client.json `telegram_user_ids` field directly, "
            "edit that file to remove them."
        )
        return
    await update.message.reply_text(
        f"Approved customers ({len(by_user)}). Tap to revoke:",
        reply_markup=_build_revoke_user_list_markup(
            config.project_root, by_user=by_user
        ),
    )


async def revoke_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline-button taps for the /unapprove flow."""
    query = update.callback_query
    if query is None or query.data is None:
        return
    config: BotConfig = context.application.bot_data["config"]
    actor_id = update.effective_user.id if update.effective_user else None
    if actor_id is None or actor_id not in config.admin_user_ids:
        await query.answer("Admin only.", show_alert=True)
        return

    data = query.data
    if data in (CB_REVOKE_REFRESH, CB_REVOKE_BACK):
        by_user = _all_approved_user_ids(config.project_root)
        if not by_user:
            await query.answer()
            with contextlib.suppress(Exception):
                await query.edit_message_text("No approved customers.")
            return
        await query.answer()
        with contextlib.suppress(Exception):
            await query.edit_message_text(
                f"Approved customers ({len(by_user)}). Tap to revoke:",
                reply_markup=_build_revoke_user_list_markup(
                    config.project_root, by_user=by_user
                ),
            )
        return

    if data.startswith(f"{CB_REVOKE_PICK}:"):
        try:
            target = int(data.split(":", 1)[1])
        except (IndexError, ValueError):
            await query.answer("Bad payload.", show_alert=True)
            return
        known = get_known_user(config.project_root, target)
        label = format_user_label(known, target)
        clients = _all_approved_user_ids(config.project_root).get(target, [])
        await query.answer()
        with contextlib.suppress(Exception):
            await query.edit_message_text(
                f"Revoke {label} from {', '.join(clients) or '(none)'}?",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✓ Yes, revoke",
                                callback_data=f"{CB_REVOKE_CONFIRM}:{target}",
                            ),
                            InlineKeyboardButton("← Back", callback_data=CB_REVOKE_BACK),
                        ],
                    ]
                ),
            )
        return

    if data.startswith(f"{CB_REVOKE_CONFIRM}:"):
        try:
            target = int(data.split(":", 1)[1])
        except (IndexError, ValueError):
            await query.answer("Bad payload.", show_alert=True)
            return
        removed = revoke_telegram_user(config.project_root, target)
        known = get_known_user(config.project_root, target)
        label = format_user_label(known, target)
        await query.answer("Revoked." if removed else "Nothing to do.")
        with contextlib.suppress(Exception):
            if removed:
                await query.edit_message_text(
                    f"✓ Revoked {label} from: {', '.join(removed)}."
                )
            else:
                await query.edit_message_text(
                    f"{label} was not on any local access list."
                )
        return

    await query.answer("Unknown action.", show_alert=True)


def _share_bot_link_markup(
    bot_username: str | None, customer_name: str | None = None
) -> InlineKeyboardMarkup | None:
    """Inline button that opens Telegram's share dialog with the bot's t.me link.

    Tapping it pops Telegram's chat-picker (the user's recent chats sit at the
    top — usually one tap to find the right person). The share message text is
    pre-filled, addressed to the customer by name when we know it, so the
    admin doesn't have to type anything.
    """
    if not bot_username:
        return None
    bot_url = f"https://t.me/{bot_username}"
    greeting = f"Hi {customer_name}, " if customer_name else "Hi, "
    # Don't embed the URL in the text — Telegram already renders it as a
    # link preview from the `url=` parameter, and including it twice doubles
    # the message length.
    text = (
        f"{greeting}I added you to my IFTA filing bot. "
        "Tap the link below and hit Start."
    )
    # quote() encodes spaces as %20. Telegram's share dialog decodes %20 to
    # spaces correctly; quote_plus() would encode them as `+` which Telegram
    # leaves as literal `+` in the message body.
    share_url = (
        f"https://t.me/share/url?url={quote(bot_url, safe='')}"
        f"&text={quote(text, safe='')}"
    )
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📤 Send them the bot link", url=share_url)]]
    )


async def _try_notify_new_customer(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    client_name: str,
    customer_label: str | None = None,
) -> tuple[bool, str, InlineKeyboardMarkup | None]:
    """DM a newly-approved customer.

    Returns (delivered, status_text, optional inline keyboard).

    Telegram blocks bots from initiating conversations with users who have
    never DMed them. When that happens we hand the admin a one-tap share
    button instead of silently claiming success.
    """
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"Access approved for {client_name}.\n\n"
                "Start your quarter upload with:\n"
                f"/new {current_filing_quarter()}"
            ),
            reply_markup=_approved_keyboard(),
        )
    except Forbidden:
        bot_username = getattr(context.bot, "username", None)
        markup = _share_bot_link_markup(bot_username, customer_label)
        status = (
            "⚠️ Couldn't DM them — they have to /start the bot first.\n"
            "Tap the button below to send them the link from your account."
        )
        if markup is None:
            status += (
                f"\nLink: t.me/{bot_username}"
                if bot_username
                else "\n(bot username unavailable — check your BotFather setup)"
            )
        return False, status, markup
    except TelegramError as e:
        return False, f"⚠️ Notify failed: {e}", None
    return True, "They've been notified.", None


# ─── Contact / forward / phone-verify flows ───────────────────────────────


def _build_client_picker_for_contact(
    project_root: Path,
    *,
    callback_prefix: str,
    arg: str,
) -> InlineKeyboardMarkup:
    """Client picker that supports any string arg (user_id or phone-without-plus)."""
    registry = load_registry(project_root)
    rows: list[list[InlineKeyboardButton]] = []
    for rec in registry.values():
        rows.append(
            [
                InlineKeyboardButton(
                    f"{rec.name} ({rec.client_id})",
                    callback_data=f"{callback_prefix}:{arg}:{rec.client_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("✕ Cancel", callback_data=CB_CANCEL)])
    return InlineKeyboardMarkup(rows)


async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatch a shared contact: admin-side onboarding vs customer-side verify."""
    config: BotConfig = context.application.bot_data["config"]
    msg = update.message
    if msg is None or msg.contact is None:
        return
    if await _reply_private_chat_error(update):
        return
    actor = update.effective_user
    if actor is None:
        return

    contact = msg.contact
    phone = normalize_phone(contact.phone_number)
    target_uid = contact.user_id
    full_name = " ".join(
        bit for bit in (contact.first_name, contact.last_name) if bit
    ) or None
    is_admin = actor.id in config.admin_user_ids
    is_self_share = target_uid is not None and target_uid == actor.id

    # Customer is sharing their OWN contact → verify against phone preauth.
    if is_self_share or (not is_admin and target_uid is None):
        await _handle_customer_phone_verify(
            update,
            context,
            phone=phone,
            target_uid=actor.id,
        )
        return

    if not is_admin:
        # Non-admin sharing someone else's contact — odd, just ignore.
        await msg.reply_text(
            "That's not your own contact. To verify yourself, use the "
            "'Share my contact' button after /start."
        )
        return

    # Admin sharing a customer's contact.
    if target_uid is not None:
        # Telegram resolved the user — we can approve directly.
        upsert_known_user(
            config.project_root,
            user_id=target_uid,
            username=None,
            name=full_name,
        )
        label = format_user_label(get_known_user(config.project_root, target_uid), target_uid)
        await msg.reply_text(
            f"Approve {label} for which carrier?",
            reply_markup=_build_client_picker_for_contact(
                config.project_root,
                callback_prefix=CB_CONTACT_APPROVE,
                arg=str(target_uid),
            ),
        )
        return

    # No Telegram id — fall back to phone preauth.
    if phone is None:
        await msg.reply_text(
            "Couldn't read this contact (no phone, no Telegram id). "
            "Ask them to /start the bot instead."
        )
        return
    await msg.reply_text(
        f"Couldn't resolve Telegram id for {full_name or phone}.\n"
        "I'll save a phone preauth — pick the carrier. The customer auto-approves "
        "when they DM the bot and tap 'Share my contact'.",
        reply_markup=_build_client_picker_for_contact(
            config.project_root,
            callback_prefix=CB_PHONE_PREAUTH,
            arg=phone.lstrip("+"),
        ),
    )


async def _handle_customer_phone_verify(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    phone: str | None,
    target_uid: int,
) -> None:
    """Customer shared their own contact → match against preauth_by_phone."""
    config: BotConfig = context.application.bot_data["config"]
    msg = update.message
    if msg is None:
        return
    if phone is None:
        await msg.reply_text(
            "Thanks, but I couldn't read a phone number from that. "
            "Use /id and send your Telegram id to Eugene."
        )
        return
    entry = get_phone_preauth(config.project_root, phone)
    if entry is None:
        await msg.reply_text(
            "Thanks for verifying. I don't have a pre-approval matching this phone.\n"
            "Use /id and send your Telegram id to Eugene."
        )
        return

    client_id = str(entry["client_id"])
    try:
        rec = approve_telegram_user(
            project_root=config.project_root,
            user_id=target_uid,
            requested_client=client_id,
        )
    except (AuthorizationError, ValueError) as e:
        await msg.reply_text(
            f"Couldn't auto-approve: {e}. Ask Eugene to approve manually."
        )
        return

    remove_phone_preauth(config.project_root, phone)
    remove_pending_user(config.project_root, target_uid)
    user = update.effective_user
    if user is not None:
        upsert_known_user(
            config.project_root,
            user_id=target_uid,
            username=user.username,
            name=user.full_name or None,
        )
    await msg.reply_text(
        f"✓ Verified and approved for {rec.name}.\n\n"
        f"Start your quarter upload with /new {current_filing_quarter()}.",
        reply_markup=_approved_keyboard(),
    )
    # DM the admin(s) so they see the new arrival.
    known_label = format_user_label(
        get_known_user(config.project_root, target_uid), target_uid
    )
    for admin_id in config.admin_user_ids:
        with contextlib.suppress(Exception):
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"✓ {known_label} verified via phone {phone} and was "
                    f"auto-approved for {rec.name} ({rec.client_id})."
                ),
            )


async def contact_flow_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Inline-button taps for the contact-share + phone-preauth flows."""
    query = update.callback_query
    if query is None or query.data is None:
        return
    config: BotConfig = context.application.bot_data["config"]
    actor_id = update.effective_user.id if update.effective_user else None
    if actor_id is None or actor_id not in config.admin_user_ids:
        await query.answer("Admin only.", show_alert=True)
        return

    data = query.data

    if data.startswith(f"{CB_CONTACT_APPROVE}:"):
        parts = data.split(":", 2)
        if len(parts) != 3:
            await query.answer("Bad payload.", show_alert=True)
            return
        try:
            target = int(parts[1])
        except ValueError:
            await query.answer("Bad payload.", show_alert=True)
            return
        client_id = parts[2]
        try:
            rec = approve_telegram_user(
                project_root=config.project_root,
                user_id=target,
                requested_client=client_id,
            )
        except (AuthorizationError, ValueError) as e:
            await query.answer(str(e), show_alert=True)
            return
        remove_pending_user(config.project_root, target)
        known = get_known_user(config.project_root, target)
        label = format_user_label(known, target)
        customer_first = known.name.split(" ")[0] if known and known.name else None
        _, status, share_markup = await _try_notify_new_customer(
            context,
            chat_id=target,
            client_name=rec.name,
            customer_label=customer_first,
        )
        await query.answer("Approved.")
        with contextlib.suppress(Exception):
            await query.edit_message_text(
                f"✓ Approved {label} for {rec.name} ({rec.client_id}).\n\n{status}",
                reply_markup=share_markup,
            )
        return

    if data.startswith(f"{CB_PHONE_PREAUTH}:"):
        parts = data.split(":", 2)
        if len(parts) != 3:
            await query.answer("Bad payload.", show_alert=True)
            return
        phone_digits = parts[1]
        client_id = parts[2]
        phone = f"+{phone_digits}"
        registry = load_registry(config.project_root)
        if client_id not in registry:
            await query.answer("Unknown client.", show_alert=True)
            return
        try:
            set_phone_preauth(
                config.project_root, phone, client_id, name=None
            )
        except ValueError as e:
            await query.answer(str(e), show_alert=True)
            return
        rec = registry[client_id]
        await query.answer("Saved.")
        with contextlib.suppress(Exception):
            await query.edit_message_text(
                f"✓ Phone preauth saved: {phone} → {rec.name} ({rec.client_id}).\n"
                "They auto-approve when they DM the bot and tap 'Share my contact'."
            )
        return

    await query.answer("Unknown action.", show_alert=True)


async def forwarded_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Admin forwards a message from a customer → add them to /pending."""
    config: BotConfig = context.application.bot_data["config"]
    msg = update.message
    if msg is None:
        return
    if await _reply_private_chat_error(update):
        return
    actor = update.effective_user
    if actor is None or actor.id not in config.admin_user_ids:
        return  # ignore non-admin forwards silently

    origin = getattr(msg, "forward_origin", None)
    if origin is None:
        return
    # PTB 22: MessageOriginUser exposes sender_user; HiddenUser exposes only
    # sender_user_name (no id — privacy-protected forward).
    sender_user = getattr(origin, "sender_user", None)
    if sender_user is None:
        sender_name = (
            getattr(origin, "sender_user_name", None)
            or getattr(origin, "author_signature", None)
            or "this user"
        )
        await msg.reply_text(
            f"{sender_name} has private forwards — I can't see their Telegram id. "
            "Ask them to send you 'Share Contact' from their Telegram, then "
            "forward that contact to me. Or send them this bot's t.me link to "
            "/start themselves."
        )
        return

    add_pending_user(
        config.project_root,
        user_id=sender_user.id,
        username=sender_user.username,
        name=sender_user.full_name or None,
    )
    upsert_known_user(
        config.project_root,
        user_id=sender_user.id,
        username=sender_user.username,
        name=sender_user.full_name or None,
    )
    label = format_user_label(
        get_known_user(config.project_root, sender_user.id), sender_user.id
    )
    await msg.reply_text(
        f"Added {label} to /pending. Tap /pending to approve."
    )


async def onboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /onboard <client_id> [Display Name] [base_state]."""
    from ifta.client import ScaffoldError, scaffold_client

    config: BotConfig = context.application.bot_data["config"]
    user_id = _effective_user_id(update)
    if update.message is None or user_id is None:
        return
    if await _reply_private_chat_error(update):
        return
    if user_id not in config.admin_user_ids:
        await update.message.reply_text("Admin only.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /onboard <client_id> [Display Name] [BASE_STATE]\n\n"
            "Example: /onboard abc_trucking ABC TRUCKING LLC TX"
        )
        return
    client_id = args[0]
    # Last token is treated as base_state if it's a 2-letter code; everything
    # between the id and the optional base_state is the display name.
    base_state: str | None = None
    rest = list(args[1:])
    if rest and len(rest[-1]) == 2 and rest[-1].isalpha():
        base_state = rest.pop().upper()
    display_name = " ".join(rest).strip() or None

    try:
        result = scaffold_client(
            config.project_root,
            client_id,
            name=display_name,
            base_state=base_state,
        )
    except ScaffoldError as e:
        await update.message.reply_text(str(e))
        return

    msg = [
        f"✓ Client '{result.client_id}' scaffolded.",
        f"Name: {result.display_name}",
    ]
    if base_state:
        msg.append(f"Base state: {base_state}")
    if result.dropped_chars:
        msg.append(
            f"Note: dropped chars {result.dropped_chars!r} during normalization."
        )
    msg.append("")
    msg.append(
        f"Next: pre-authorize the customer with\n"
        f"  /preauth @their_username {result.client_id}\n"
        "or approve their id directly after they DM the bot."
    )
    await update.message.reply_text("\n".join(msg))


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
        await update.message.reply_text(f"Usage: /new {current_filing_quarter()} [client_id]")
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
        "When both parse cleanly, send /process.",
        reply_markup=_approved_keyboard(submission.quarter),
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: BotConfig = context.application.bot_data["config"]
    if update.message is None:
        return
    if await _reply_private_chat_error(update):
        return
    _user_data(context).pop("submission", None)
    await update.message.reply_text(
        "Current upload session cancelled. Saved files were not deleted.",
        reply_markup=_keyboard_from_context(update, config),
    )


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
        await update.message.reply_text(
            f"No active submission. Start with /new {current_filing_quarter()}.",
            reply_markup=_keyboard_from_context(update, config),
        )
        return
    report = preflight_inputs(submission.inbox)
    identity = check_client_identity(
        project_root=config.project_root,
        submission=submission,
        report=report,
    )
    await update.message.reply_text(
        f"{submission.client_name} {submission.quarter}\n\n{summarize_preflight(report, identity)}",
        reply_markup=_approved_keyboard(submission.quarter),
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
        await update.message.reply_text(
            f"Start an upload first: /new {current_filing_quarter()} [client_id]"
        )
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
        f"Saved {destination.name}.\n\n{summarize_preflight(report, identity)}{next_line}",
        reply_markup=_approved_keyboard(submission.quarter),
    )


async def process_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: BotConfig = context.application.bot_data["config"]
    notifier: AdminNotifier = context.application.bot_data["admin_notifier"]
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
        await update.message.reply_text(
            f"No active submission. Start with /new {current_filing_quarter()}."
        )
        return
    if not submission.inbox.exists():
        await update.message.reply_text(f"Inbox not found: {submission.inbox}")
        return

    customer_label = _customer_label(update, submission)

    await update.message.reply_text(
        f"Processing {submission.client_name} {submission.quarter}. This can take a few minutes."
    )
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        result = await asyncio.to_thread(run_delivery, submission, config)
    except DeliveryBlockedError as e:
        await update.message.reply_text(f"Processing blocked this submission:\n\n{e}")
        _safe_admin_notify(
            notifier,
            headline="❌ IFTA submission blocked",
            source="telegram bot",
            customer=customer_label,
            quarter=submission.quarter,
            extras={"Client": submission.client_name, "Error": str(e)},
        )
        return
    except Exception as e:
        await update.message.reply_text(f"Processing failed: {type(e).__name__}: {e}")
        _safe_admin_notify(
            notifier,
            headline="❌ IFTA submission failed",
            source="telegram bot",
            customer=customer_label,
            quarter=submission.quarter,
            extras={"Client": submission.client_name, "Error": f"{type(e).__name__}: {e}"},
        )
        return

    _safe_admin_notify(
        notifier,
        headline="✅ IFTA packet delivered",
        source="telegram bot",
        customer=customer_label,
        quarter=submission.quarter,
        extras={"Client": submission.client_name},
        review_note_path=result.review_note,
    )

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
        "I am sending the customer packet now. Review before submitting to the portal.",
        reply_markup=_approved_keyboard(result.quarter),
    )

    for path in result.customer_files:
        if not path.exists():
            continue
        await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
        with path.open("rb") as fh:
            await update.message.reply_document(document=fh, filename=path.name)


def _customer_label(update: Update, submission: Submission) -> str:
    """Best-effort identifier for the requesting Telegram user in admin messages."""
    user = update.effective_user
    parts: list[str] = []
    if user is not None:
        if user.username:
            parts.append(f"@{user.username}")
        elif user.full_name:
            parts.append(user.full_name)
        parts.append(f"id={user.id}")
    if not parts:
        parts.append("unknown user")
    return " ".join(parts)


def _safe_admin_notify(
    notifier: AdminNotifier,
    *,
    headline: str,
    source: str,
    customer: str,
    quarter: str | None = None,
    extras: dict[str, str] | None = None,
    review_note_path: Path | None = None,
) -> None:
    """Wrap admin notify so a network blip never crashes the bot handler."""
    # Notifier already logs; swallow here so customer-facing flow continues.
    with contextlib.suppress(Exception):
        notifier.send(
            format_event(
                headline=headline,
                source=source,
                customer=customer,
                quarter=quarter,
                extras=extras,
                review_note_path=review_note_path,
            )
        )


async def web_approval_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle Accept/Decline taps for web-intake submissions.

    The approval card is sent by the FastAPI app via ``TelegramApprovalClient``.
    This handler runs inside the existing ``ifta telegram-bot`` polling loop so
    no separate webhook or bot process is needed.
    """
    query = update.callback_query
    if query is None or query.data is None:
        return
    config: BotConfig = context.application.bot_data["config"]
    actor_id = update.effective_user.id if update.effective_user else None
    if actor_id is None or actor_id not in config.admin_user_ids:
        await query.answer("Admin only.", show_alert=True)
        return

    data = query.data
    is_accept = data.startswith(f"{CB_WEB_ACCEPT}:")
    is_decline = data.startswith(f"{CB_WEB_DECLINE}:")
    is_more_files = data.startswith(f"{CB_WEB_MORE_FILES}:")
    if not (is_accept or is_decline or is_more_files):
        await query.answer("Unknown action.", show_alert=True)
        return

    submission_id = data.split(":", 1)[1] if ":" in data else ""
    if not submission_id:
        await query.answer("Bad payload.", show_alert=True)
        return

    # Lazy imports to avoid circular deps and keep the import cost out of
    # unrelated code paths.
    from ifta.web import db as web_db
    from ifta.web.app import get_db_path
    from ifta.web.email import EmailClient, load_email_config_from_env
    from ifta.web.telegram_approval import TelegramApprovalClient, load_approval_config

    db_path = get_db_path()
    web_db.init_db(db_path)
    sub = web_db.get_submission(db_path, submission_id)
    if sub is None:
        await query.answer("Submission not found.", show_alert=True)
        return

    from ifta.web.models import SubmissionStatus as WebSubmissionStatus

    actor_user = update.effective_user
    decided_by = "unknown"
    if actor_user is not None:
        parts = [p for p in [actor_user.first_name, actor_user.last_name] if p]
        decided_by = " ".join(parts) or f"id={actor_user.id}"

    approval_client = TelegramApprovalClient(load_approval_config())
    email_client = EmailClient(load_email_config_from_env())
    chat_id = query.message.chat.id if query.message else None
    message_id = query.message.message_id if query.message else None

    if is_accept:
        if sub.status != WebSubmissionStatus.PENDING_APPROVAL:
            await query.answer(
                f"Already decided ({sub.status.value}).", show_alert=True
            )
            return
        sub = web_db.approve_submission(db_path, submission_id, decided_by=decided_by)
        if sub is None:
            await query.answer("DB error.", show_alert=True)
            return

        # Edit the Telegram card in-place.
        if chat_id is not None and message_id is not None:
            with contextlib.suppress(Exception):
                approval_client.edit_card_approved(chat_id, message_id, sub, decided_by)

        # Send acknowledgement that processing will start.
        with contextlib.suppress(Exception):
            email_client.send_acknowledgement(sub)

        await query.answer("Approved -- queued for processing.")
        return

    # Request-more-files flow (Step 8 slice 3): mark the submission as
    # NEEDS_MORE_FILES, edit the card in place, and email the customer a
    # plain-English ask backed by the intake brief we already wrote to disk
    # at submit time.
    if is_more_files:
        if sub.status != WebSubmissionStatus.PENDING_APPROVAL:
            await query.answer(
                f"Already decided ({sub.status.value}).", show_alert=True
            )
            return
        sub = web_db.request_more_files_submission(
            db_path, submission_id, decided_by=decided_by,
        )
        if sub is None:
            await query.answer("DB error.", show_alert=True)
            return

        if chat_id is not None and message_id is not None:
            with contextlib.suppress(Exception):
                approval_client.edit_card_more_files_requested(
                    chat_id, message_id, sub, decided_by,
                )

        # Load the intake brief from disk to drive the friendly email body.
        # Falls back to a generic email if the brief is missing.
        intake_brief_text = ""
        if sub.intake_brief_path:
            brief_path = config.project_root / sub.intake_brief_path
            with contextlib.suppress(Exception):
                intake_brief_text = brief_path.read_text(encoding="utf-8")
        with contextlib.suppress(Exception):
            email_client.send_more_files_request(sub, intake_brief_text)

        await query.answer("Customer asked for more files.")
        return

    # Decline flow: ask for a reason via a follow-up message.
    if sub.status != WebSubmissionStatus.PENDING_APPROVAL:
        await query.answer(
            f"Already decided ({sub.status.value}).", show_alert=True
        )
        return

    # For simplicity, use a default reason. A richer UX would prompt, but
    # that requires conversational state in the bot. Use a short default and
    # let the admin type /decline <id> <reason> for custom messages.
    reason = "Does not meet filing requirements. Please contact hello@artjeck.com."
    sub = web_db.reject_submission(
        db_path, submission_id, decided_by=decided_by, reason=reason,
    )
    if sub is None:
        await query.answer("DB error.", show_alert=True)
        return

    if chat_id is not None and message_id is not None:
        with contextlib.suppress(Exception):
            approval_client.edit_card_rejected(
                chat_id, message_id, sub, decided_by, reason,
            )

    with contextlib.suppress(Exception):
        email_client.send_rejection(sub, reason)

    await query.answer("Declined -- customer notified.")


def build_application(config: BotConfig) -> Application:
    app = ApplicationBuilder().token(config.token).build()
    app.bot_data["config"] = config
    app.bot_data["admin_notifier"] = AdminNotifier(load_admin_notifier_config())
    # group=-1 runs before all default-group handlers — used to track first-time
    # DMs and auto-approve preauthorized usernames.
    app.add_handler(
        MessageHandler(filters.ALL, _first_contact_middleware), group=-1
    )
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("request", request_access_command))
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("add", approve_command))  # /add is an alias.
    app.add_handler(CommandHandler("clients", clients_command))
    app.add_handler(CommandHandler("pending", pending_command))
    app.add_handler(CommandHandler("preauth", preauth_command))
    app.add_handler(CommandHandler("unapprove", unapprove_command))
    app.add_handler(CommandHandler("onboard", onboard_command))
    app.add_handler(CommandHandler("new", new_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("process", process_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    # Contact share (admin onboarding Nastya, or customer verifying their phone).
    app.add_handler(MessageHandler(filters.CONTACT, contact_handler))
    # Forwarded message from a customer — extract sender, add to /pending.
    app.add_handler(MessageHandler(filters.FORWARDED, forwarded_handler))
    # Inline-keyboard callbacks (admin wizards). Pattern matched at registration
    # time so they only fire for our own callback_data prefixes.
    app.add_handler(
        CallbackQueryHandler(
            pending_callback,
            pattern=rf"^(?:{CB_PENDING_PICK_USER}:|{CB_PENDING_PICK_CLIENT}:|{CB_PENDING_IGNORE}:|{CB_PENDING_REFRESH}$|{CB_PENDING_BACK}$|{CB_CANCEL}$)",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            revoke_callback,
            pattern=rf"^(?:{CB_REVOKE_PICK}:|{CB_REVOKE_CONFIRM}:|{CB_REVOKE_REFRESH}$|{CB_REVOKE_BACK}$)",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            contact_flow_callback,
            pattern=rf"^(?:{CB_CONTACT_APPROVE}:|{CB_PHONE_PREAUTH}:)",
        )
    )
    # Web-intake submission approval (Accept / Decline buttons sent by the
    # FastAPI app via TelegramApprovalClient).
    app.add_handler(
        CallbackQueryHandler(
            web_approval_callback,
            pattern=rf"^(?:{CB_WEB_ACCEPT}:|{CB_WEB_DECLINE}:|{CB_WEB_MORE_FILES}:)",
        )
    )
    return app


def run_polling(config: BotConfig) -> None:
    app = build_application(config)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
