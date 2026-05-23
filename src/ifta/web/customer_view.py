"""Human-readable IFTA packet email body for the customer.

The structured ReviewNote the agent produces and the deterministic findings
list are great for audit and Telegram alerts, but they're developer output:
SHOUTY_CODES, JSON evidence blobs, "Filing impact:" headers. A trucker or
their accountant opens an email like that and gets confused or scared off.

This module renders the SAME underlying facts as short, plain-English copy
fit for a phone screen. No codes. No JSON. No markdown headers. Just:

    Hi {Name},

    Your Q1-2026 IFTA packet is ready. Looks ready to file.

    Total tax due: $15.75
    Fleet MPG: 6.67

    Before you file, please double-check:
    • <plain claim 1>
    • <plain claim 2>

    Attached:
    • ifta_portal.csv ...

The dev-facing review_note.md is left untouched — operator-side audit, the
admin Telegram alert, and the AI's reasoning chain still depend on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ifta.calc import IftaReturn
from ifta.validator import Finding
from ifta.web.models import Submission

CUSTOMER_NOTE_FILENAME = "customer_note.md"

_FILING_STATUS_LINE = {
    "READY_TO_FILE": "Looks ready to file.",
    "READY_WITH_WARNINGS": "Please double-check the items below before filing.",
    "DO_NOT_FILE": "We found issues — please don't file yet. See below.",
}


@dataclass(frozen=True)
class _Action:
    """One plain-English bullet point we'll show the customer."""

    text: str

    def key(self) -> str:
        """Loose dedup key — normalized lowercase comparison."""
        return re.sub(r"\s+", " ", self.text.lower()).strip()


def render_customer_view(
    *,
    sub: Submission,
    ret: IftaReturn,
    note: Any | None = None,
    findings: list[Finding] | None = None,
    truck_count: int = 0,
) -> str:
    """Render the friendly customer-facing email body for a finished packet.

    Pass `note` (the agent's ReviewNote) when available; otherwise pass
    `findings` (the deterministic validator output). Both are optional — with
    neither, we still produce a minimal status email.
    """
    greeting = f"Hi {sub.name}," if sub.name else "Hi,"
    status_blurb = _status_blurb(note)

    lines: list[str] = [greeting, ""]
    lines.append(f"Your {sub.quarter} IFTA packet is ready. {status_blurb}")
    lines.append("")
    lines.append(f"Total tax due: ${ret.total_tax_due:,.2f}")
    lines.append(f"Fleet MPG: {ret.fleet_mpg:.2f}")
    lines.append("")

    actions = _collect_actions(note=note, findings=findings)
    if actions:
        lines.append("Before you file, please double-check:")
        for a in actions:
            lines.append(f"• {a.text}")
        lines.append("")

    if ret.rate_warning:
        # A blocking rate-fallback condition — customer must know not to file yet.
        lines.append(f"⚠️  Heads up: {ret.rate_warning}")
        lines.append("")

    lines.append("Attached:")
    lines.append("• ifta_portal.csv — upload this directly to your state's IFTA portal")
    if truck_count:
        if truck_count == 1:
            lines.append("• trucks/<id>.xlsx — per-truck breakdown")
        else:
            lines.append(
                f"• trucks/<id>.xlsx — per-truck breakdown ({truck_count} trucks)"
            )
    lines.append("")
    lines.append("Questions? Just reply — Eugene reads every email.")
    lines.append("")
    lines.append("— ArtJeck IFTA")
    return "\n".join(lines) + "\n"


# ─── internals ────────────────────────────────────────────────────────────────


def _status_blurb(note: Any | None) -> str:
    if note is None:
        return "Please review the attached files before filing."
    status = getattr(note, "filing_status", None) or ""
    return _FILING_STATUS_LINE.get(status, "Please review the attached files before filing.")


def _collect_actions(
    *, note: Any | None, findings: list[Finding] | None
) -> list[_Action]:
    """Pull warning/error claims out of an agent note OR validator findings.

    Returns a deduped, order-preserving list of plain-English bullets.
    Strips code-like artifacts (CODE: prefixes, Evidence/Impact tails) so the
    customer never sees developer output.
    """
    actions: list[_Action] = []
    seen: set[str] = set()

    def push(text: str | None) -> None:
        if not text:
            return
        cleaned = _humanize(text)
        if not cleaned:
            return
        a = _Action(text=cleaned)
        if a.key() in seen:
            return
        seen.add(a.key())
        actions.append(a)

    if note is not None:
        for item in (getattr(note, "issues", None) or []):
            if not _is_actionable(item):
                continue
            push(_get(item, "claim"))
        for item in (getattr(note, "next_steps", None) or []):
            if not _is_actionable(item):
                continue
            push(_get(item, "recommended_action") or _get(item, "claim"))
    elif findings:
        for f in findings:
            if f.severity not in ("warning", "error"):
                continue
            push(f.message)

    return actions


def _is_actionable(item: Any) -> bool:
    """Skip plain strings and info-level entries — we only show things that
    require the customer to do or check something."""
    if not isinstance(item, dict):
        return False
    severity = str(item.get("severity") or "").strip().lower()
    return severity in ("warning", "error")


def _get(item: dict, key: str) -> str:
    val = item.get(key)
    return str(val).strip() if val is not None else ""


# Patterns that strip developer markup out of agent claim text. These are the
# concrete things we saw leaking into the customer-facing email today:
#   "[warning] CODE: claim ..."
#   "claim ... Evidence: {...} Impact: ..."
#   "MILES_SUSPICIOUSLY_FEW: claim ..."
_SEVERITY_PREFIX = re.compile(r"^\s*\[(?:warning|error|info)\]\s*", re.IGNORECASE)
_CODE_PREFIX = re.compile(r"^\s*[A-Z][A-Z0-9_]{2,}:\s*")
_EVIDENCE_TAIL = re.compile(r"\s*Evidence:\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)
_IMPACT_TAIL = re.compile(r"\s*Impact:\s*.+?(?=$|Evidence:)", re.DOTALL)
_SOURCE_TAIL = re.compile(r"\s*\(source:[^)]*\)", re.IGNORECASE)


def _humanize(text: str) -> str:
    """Strip the dev-style metadata so only the human-readable claim is left."""
    out = text
    out = _EVIDENCE_TAIL.sub("", out)
    out = _IMPACT_TAIL.sub("", out)
    out = _SOURCE_TAIL.sub("", out)
    out = _SEVERITY_PREFIX.sub("", out)
    out = _CODE_PREFIX.sub("", out)
    # Collapse internal whitespace + tidy.
    out = re.sub(r"\s+", " ", out).strip().rstrip(".")
    return out + "." if out and not out.endswith((".", "!", "?")) else out
