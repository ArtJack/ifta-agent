"""Client/quarter context resolution.

The pipeline can still run the original simple layout:

    inbox/Q2-2026/

For multi-client production work it also understands:

    inbox/<client_id>/Q2-2026/

Each quarter folder can include `client.json` to identify the carrier. This
keeps the agent from silently applying another client's profile.

Client registry is loaded from `data/clients/<client_id>/client.json` — adding
a new customer is a folder + JSON file, no code changes.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

_QUARTER_RE = re.compile(r"^Q[1-4]-\d{4}$")


def quarter_key(quarter: str) -> str:
    """Normalize a quarter label to canonical 'Q<n>-YYYY' form.

    Accepts 'Q4-2025', 'Q4 2025', '4Q2025', 'q4_2025'. Rejects empty input,
    invalid quarter numbers (Q0/Q5+), missing year, or otherwise unparsable
    strings — so downstream code can trust the value rather than producing
    confusing 'inbox not found' errors for typos.
    """
    if quarter is None or not quarter.strip():
        raise ValueError("Quarter is required (e.g. 'Q4-2025').")

    raw = quarter.strip().upper().replace(" ", "-").replace("_", "-")
    # Accept 'NQYYYY' shape too — flip to 'QN-YYYY'.
    flipped = re.match(r"^(\d)Q-?(\d{4})$", raw.replace("-", ""))
    if flipped:
        raw = f"Q{flipped.group(1)}-{flipped.group(2)}"
    elif "-" not in raw and re.match(r"^Q\d\d{4}$", raw):
        raw = f"{raw[:2]}-{raw[2:]}"

    if not _QUARTER_RE.match(raw):
        raise ValueError(
            f"Invalid quarter {quarter!r}. Expected 'Q<1-4>-YYYY' "
            f"(e.g. 'Q4-2025') or '<1-4>Q<YYYY>' (e.g. '4Q2025')."
        )
    return raw


# Calendar quarters → (start month, end month, end day).
_QUARTER_BOUNDS: dict[str, tuple[int, int, int]] = {
    "Q1": (1, 3, 31),
    "Q2": (4, 6, 30),
    "Q3": (7, 9, 30),
    "Q4": (10, 12, 31),
}

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]  # fmt: skip


def quarter_dates(quarter: str) -> tuple[str, str] | tuple[None, None]:
    """Return (start_date_str, end_date_str) for a quarter key.

    Accepts forms like "Q2-2026" or "2Q2026". Returns ("April 1, 2026",
    "June 30, 2026") for Q2. Returns (None, None) if it can't parse.
    """
    key = quarter_key(quarter)
    qmatch = re.match(r"^Q(\d)-?(\d{4})$|^(\d)Q(\d{4})$", key.replace("-", ""))
    if not qmatch:
        return None, None
    q = qmatch.group(1) or qmatch.group(3)
    y = qmatch.group(2) or qmatch.group(4)
    bounds = _QUARTER_BOUNDS.get(f"Q{q}")
    if bounds is None:
        return None, None
    start_month, end_month, end_day = bounds
    start = f"{_MONTH_NAMES[start_month - 1]} 1, {y}"
    end = f"{_MONTH_NAMES[end_month - 1]} {end_day}, {y}"
    return start, end


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_") or "unknown"


# ---------------------------------------------------------------------------
# Client registry — loaded from data/clients/<id>/client.json
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClientRecord:
    """A registered client. Read from data/clients/<id>/client.json."""

    client_id: str
    name: str
    aliases: tuple[str, ...] = ()
    base_jurisdiction: str | None = None
    portal: str = "generic"
    profile: str = "none"
    source_folder: str | None = None
    profile_path: str | None = "profile.json"
    history_path: str | None = "history.json"
    active: bool = False
    notes: str = ""
    record_dir: Path | None = None
    # Telegram numeric user IDs that may submit filings on behalf of this
    # client via the bot. The bot rejects any sender not in this list.
    telegram_user_ids: tuple[int, ...] = ()

    def resolve_path(self, attr: str) -> Path | None:
        """Resolve profile_path / history_path / source_folder against record_dir."""
        value = getattr(self, attr)
        if not value or self.record_dir is None:
            return None
        p = Path(value)
        return p if p.is_absolute() else (self.record_dir / p).resolve()


def _registry_dir(project_root: Path) -> Path:
    return project_root / "data" / "clients"


@lru_cache(maxsize=32)
def _load_registry_cached(registry_dir_str: str) -> dict[str, ClientRecord]:
    registry_dir = Path(registry_dir_str)
    records: dict[str, ClientRecord] = {}
    if not registry_dir.exists():
        return records
    for client_dir in sorted(registry_dir.iterdir()):
        if not client_dir.is_dir() or client_dir.name.startswith("."):
            continue
        meta = client_dir / "client.json"
        if not meta.exists():
            continue
        payload = json.loads(meta.read_text(encoding="utf-8"))
        cid = _slugify(str(payload.get("client_id") or client_dir.name))
        records[cid] = ClientRecord(
            client_id=cid,
            name=str(payload.get("name") or cid),
            aliases=tuple(str(a) for a in payload.get("aliases", [])),
            base_jurisdiction=payload.get("base_jurisdiction"),
            portal=str(payload.get("portal") or "generic"),
            profile=str(payload.get("profile") or "none"),
            source_folder=payload.get("source_folder"),
            telegram_user_ids=tuple(
                int(uid) for uid in payload.get("telegram_user_ids", []) if uid is not None
            ),
            profile_path=payload.get("profile_path") or "profile.json",
            history_path=payload.get("history_path") or "history.json",
            active=bool(payload.get("active", False)),
            notes=str(payload.get("notes") or ""),
            record_dir=client_dir,
        )
    return records


def load_registry(project_root: Path) -> dict[str, ClientRecord]:
    """All registered clients, keyed by client_id."""
    return _load_registry_cached(str(_registry_dir(project_root)))


def reload_registry(project_root: Path) -> None:
    """Clear the cache — call after writing a new client.json."""
    _load_registry_cached.cache_clear()
    load_registry(project_root)


def _alias_map(registry: dict[str, ClientRecord]) -> dict[str, str]:
    out: dict[str, str] = {}
    for cid, rec in registry.items():
        out[cid] = cid
        out[_slugify(rec.name)] = cid
        for alias in rec.aliases:
            out[_slugify(alias)] = cid
    return out


def normalize_client_id(value: str, project_root: Path | None = None) -> str:
    """Map a free-text client name/id to a registry id when possible."""
    slug = _slugify(value)
    if project_root is not None:
        registry = load_registry(project_root)
        alias_map = _alias_map(registry)
        if slug in alias_map:
            return alias_map[slug]
    return slug


# ---------------------------------------------------------------------------
# Client context (resolved per-quarter)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClientContext:
    client_id: str = "unknown"
    client_name: str = "Unknown client"
    base_jurisdiction: str | None = None
    portal: str = "generic"
    profile: str = "none"
    source: str = "default"
    notes: str = ""
    record_dir: Path | None = field(default=None, compare=False)

    @property
    def is_unknown(self) -> bool:
        return self.client_id == "unknown"

    def to_prompt_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("record_dir", None)
        return d


def _context_from_record(record: ClientRecord, source: str) -> ClientContext:
    return ClientContext(
        client_id=record.client_id,
        client_name=record.name,
        base_jurisdiction=record.base_jurisdiction,
        portal=record.portal,
        profile=record.profile,
        source=source,
        notes=record.notes,
        record_dir=record.record_dir,
    )


def resolve_inbox(project_root: Path, quarter: str, client: str | None = None) -> Path:
    qkey = quarter_key(quarter)
    if client:
        client_id = normalize_client_id(client, project_root)
        nested = project_root / "inbox" / client_id / qkey
        if nested.exists():
            return nested
    return project_root / "inbox" / qkey


def resolve_output_dir(project_root: Path, quarter: str, client: str | None = None) -> Path:
    qkey = quarter_key(quarter)
    if client:
        client_id = normalize_client_id(client, project_root)
        nested_inbox = project_root / "inbox" / client_id / qkey
        if nested_inbox.exists():
            return project_root / "outputs" / client_id / qkey
    return project_root / "outputs" / qkey


def _context_from_metadata(
    project_root: Path, path: Path
) -> ClientContext | None:
    metadata_path = path / "client.json"
    if not metadata_path.exists():
        return None

    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Malformed JSON in {metadata_path}: {e.msg} (line {e.lineno}, "
            f"column {e.colno}). Fix the file or remove it to fall back to "
            f"the --client argument."
        ) from e
    raw_id = str(payload.get("client_id") or payload.get("name") or "unknown")
    client_id = normalize_client_id(raw_id, project_root)
    registry = load_registry(project_root)
    known = registry.get(client_id)
    return ClientContext(
        client_id=client_id,
        client_name=str(payload.get("name") or (known.name if known else "Unknown client")),
        base_jurisdiction=payload.get("base_jurisdiction")
        or (known.base_jurisdiction if known else None),
        portal=str(payload.get("portal") or (known.portal if known else "generic")),
        profile=str(payload.get("profile") or (known.profile if known else "none")),
        source=str(metadata_path),
        notes=str(payload.get("notes") or (known.notes if known else "")),
        record_dir=known.record_dir if known else None,
    )


def load_client_context(
    project_root: Path,
    quarter: str,
    client: str | None = None,
    inbox: Path | None = None,
) -> ClientContext:
    resolved_inbox = inbox or resolve_inbox(project_root, quarter, client)
    metadata_context = _context_from_metadata(project_root, resolved_inbox)
    if metadata_context:
        return metadata_context

    if client:
        client_id = normalize_client_id(client, project_root)
        registry = load_registry(project_root)
        known = registry.get(client_id)
        if known:
            return _context_from_record(known, source="registry")
        return ClientContext(
            client_id=client_id,
            client_name=client.strip(),
            source="cli",
            notes="Client was supplied by CLI but has no registry entry.",
        )

    return ClientContext(
        source="missing",
        notes=(
            "No --client argument and no client.json found in the quarter inbox. "
            "Agent must not assume any known carrier profile."
        ),
    )


def get_client_record(project_root: Path, client_id: str) -> ClientRecord | None:
    """Look up a registered client by id (or alias)."""
    normalized = normalize_client_id(client_id, project_root)
    return load_registry(project_root).get(normalized)


@dataclass
class ScaffoldResult:
    client_id: str
    client_dir: Path
    inbox_dir: Path | None
    display_name: str
    dropped_chars: str  # non-empty if normalization dropped characters


class ScaffoldError(ValueError):
    """Raised when client scaffolding fails for a user-actionable reason."""


def scaffold_client(
    project_root: Path,
    client_id: str,
    *,
    name: str | None = None,
    base_state: str | None = None,
    portal: str = "generic",
    aliases: tuple[str, ...] = (),
    source_folder: str | None = None,
    make_inbox: bool = True,
) -> ScaffoldResult:
    """Create `data/clients/<id>/{client.json, profile.json}` for a new carrier.

    Returns ScaffoldResult on success. Raises ScaffoldError when the requested
    id collides with an existing client or normalizes to an empty string.
    """
    import json as _json
    import re as _re

    norm = _re.sub(r"[^a-z0-9]+", "_", client_id.strip().lower()).strip("_")
    if not norm:
        raise ScaffoldError("client_id must contain at least one alphanumeric character.")

    # Detect dropped characters (e.g. non-ASCII) so callers can warn the user.
    input_chars = _re.sub(r"[-_ \s]+", "", client_id.strip().lower())
    norm_chars = norm.replace("_", "")
    dropped = "".join(c for c in input_chars if c not in norm_chars) if input_chars != norm_chars else ""

    # Collision check: refuse if `client_id` resolves to an existing registry entry.
    registry = load_registry(project_root)
    resolved = normalize_client_id(client_id, project_root)
    if resolved in registry:
        existing = registry[resolved]
        raise ScaffoldError(
            f"'{client_id}' already resolves to registered client "
            f"'{existing.client_id}' (name={existing.name!r}, "
            f"aliases={list(existing.aliases)}). Pick a different id or edit "
            f"data/clients/{existing.client_id}/ directly."
        )

    client_dir = project_root / "data" / "clients" / norm
    if client_dir.exists() and (client_dir / "client.json").exists():
        raise ScaffoldError(
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
        "notes": f"Onboarded via scaffold_client('{norm}').",
    }
    (client_dir / "client.json").write_text(
        _json.dumps(client_meta, indent=2) + "\n", encoding="utf-8"
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
        _json.dumps(profile_stub, indent=2) + "\n", encoding="utf-8"
    )

    inbox_dir: Path | None = None
    if make_inbox:
        inbox_dir = project_root / "inbox" / norm
        inbox_dir.mkdir(parents=True, exist_ok=True)

    reload_registry(project_root)
    return ScaffoldResult(
        client_id=norm,
        client_dir=client_dir,
        inbox_dir=inbox_dir,
        display_name=display_name,
        dropped_chars=dropped,
    )
