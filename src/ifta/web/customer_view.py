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
CUSTOMER_SUMMARY_FILENAME = "summary_report.md"

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
    lines.append("• summary_report.md — full plain-English breakdown for your records")
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


# ─── customer summary report ─────────────────────────────────────────────────


_FILING_STATUS_HEADER = {
    "READY_TO_FILE": "Status: Ready to file",
    "READY_WITH_WARNINGS": "Status: Ready to file — please double-check the items below",
    "DO_NOT_FILE": "Status: Do NOT file yet — please resolve the items below first",
}


def render_customer_summary(
    *,
    sub: Submission,
    ret: IftaReturn,
    note: Any | None = None,
    findings: list[Finding] | None = None,
    truck_count: int = 0,
    attached_files: list[str] | None = None,
) -> str:
    """Detailed customer-readable summary report.

    Long-form companion to `render_customer_view` (which is the short email
    body). Surfaces the full picture — every problem with its claim, the
    'why it matters', and the 'what to do' — but in plain English. No
    SHOUTY_CODES, no JSON evidence, no "[warning]" prefixes.

    Designed to be attached to the customer's packet email so they (or
    their accountant) can keep it for their records and forward it.
    """
    carrier = sub.company or "your company"
    header = f"# IFTA {sub.quarter} Summary Report — {carrier}"
    status_header = _FILING_STATUS_HEADER.get(
        getattr(note, "filing_status", "") or "", "Status: Packet ready"
    )

    lines: list[str] = [
        header,
        "",
        f"_Prepared for {sub.email}._",
        "",
        f"## {status_header}",
        "",
    ]

    # ── Key numbers ────────────────────────────────────────────────────────
    lines.append("## Key numbers")
    lines.append("")
    lines.append(f"- **Total tax due:** ${ret.total_tax_due:,.2f}")
    lines.append(f"- **Fleet MPG:** {ret.fleet_mpg:.2f}  _(realistic range for heavy trucks is roughly 5–8)_")
    lines.append(f"- **Fleet miles:** {ret.fleet_miles:,.0f}")
    lines.append(f"- **Fleet gallons:** {ret.fleet_gallons:,.2f}")
    if truck_count:
        lines.append(f"- **Trucks on this return:** {truck_count}")
    lines.append("")

    # ── Rate warning (loud) ────────────────────────────────────────────────
    if ret.rate_warning:
        lines.append("## ⚠️ Rate notice")
        lines.append("")
        lines.append(ret.rate_warning)
        lines.append("")

    # ── Problems / things to check ─────────────────────────────────────────
    problems = _structured_problems(note=note, findings=findings)
    if problems:
        lines.append("## Things to double-check before filing")
        lines.append("")
        for p in problems:
            lines.append(f"### {p['headline']}")
            lines.append("")
            if p["detail"]:
                lines.append(p["detail"])
                lines.append("")
            if p["why"]:
                lines.append(f"**Why it matters:** {p['why']}")
            if p["what_to_do"]:
                lines.append(f"**What to do:** {p['what_to_do']}")
            lines.append("")

    # ── Filing tips (info-level reminders) ─────────────────────────────────
    tips = _structured_tips(note=note)
    if tips:
        lines.append("## Filing tips")
        lines.append("")
        for t in tips:
            lines.append(f"- {t}")
        lines.append("")

    # ── Attachments ────────────────────────────────────────────────────────
    if attached_files:
        lines.append("## Files in this packet")
        lines.append("")
        for name in attached_files:
            lines.append(f"- `{name}`")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("Questions? Reply to the email this report came with — Eugene reads every one.")
    lines.append("")
    return "\n".join(lines) + "\n"


def _structured_problems(
    *, note: Any | None, findings: list[Finding] | None
) -> list[dict[str, str]]:
    """Return one dict per warning/error, with headline + detail + why + what-to-do.

    Built once and shared between summary rendering and (later) any other
    detailed customer surface. Dev markup is stripped at this layer so callers
    never have to think about it.
    """
    out: list[dict[str, str]] = []
    seen_headlines: set[str] = set()

    if note is not None:
        # Build a map from issue-claim → its next_steps recommended_action so the
        # summary pairs the two without showing them as two separate problems.
        actions_by_claim_key: dict[str, str] = {}
        for step in (getattr(note, "next_steps", None) or []):
            if not _is_actionable(step):
                continue
            paired_claim = _humanize(_get(step, "claim"))
            action = _humanize(_get(step, "recommended_action"))
            if paired_claim and action:
                actions_by_claim_key[_norm(paired_claim)] = action

        for issue in (getattr(note, "issues", None) or []):
            if not _is_actionable(issue):
                continue
            claim = _humanize(_get(issue, "claim"))
            if not claim:
                continue
            headline, detail = _split_first_sentence(claim)
            if _norm(headline) in seen_headlines:
                continue
            seen_headlines.add(_norm(headline))
            why = _humanize(_get(issue, "filing_impact"))
            what_to_do = (
                actions_by_claim_key.get(_norm(claim))
                or _humanize(_get(issue, "recommended_action"))
            )
            out.append(
                {
                    "headline": headline,
                    "detail": detail,
                    "why": why,
                    "what_to_do": what_to_do,
                }
            )
            # Reserve the action text itself so the standalone next_steps
            # pass doesn't re-emit "Reply with your IFTA base state..." as
            # its own empty problem entry.
            if what_to_do:
                seen_headlines.add(_norm(_split_first_sentence(what_to_do)[0]))

        # Next steps that don't pair with an existing issue → standalone items.
        for step in (getattr(note, "next_steps", None) or []):
            if not _is_actionable(step):
                continue
            action = _humanize(_get(step, "recommended_action")) or _humanize(_get(step, "claim"))
            if not action:
                continue
            headline, detail = _split_first_sentence(action)
            if _norm(headline) in seen_headlines:
                continue
            seen_headlines.add(_norm(headline))
            out.append(
                {
                    "headline": headline,
                    "detail": detail,
                    "why": _humanize(_get(step, "filing_impact")),
                    "what_to_do": "",
                }
            )
    elif findings:
        for f in findings:
            if f.severity not in ("warning", "error"):
                continue
            headline, detail = _split_first_sentence(f.message)
            if _norm(headline) in seen_headlines:
                continue
            seen_headlines.add(_norm(headline))
            out.append({"headline": headline, "detail": detail, "why": "", "what_to_do": ""})
    return out


def _structured_tips(*, note: Any | None) -> list[str]:
    """Info-level filing_reminders rendered as plain bullets, dev markup stripped."""
    if note is None:
        return []
    tips: list[str] = []
    seen: set[str] = set()
    for item in (getattr(note, "filing_reminders", None) or []):
        if not isinstance(item, dict):
            continue
        text = _humanize(_get(item, "claim") or _get(item, "recommended_action"))
        if not text:
            continue
        key = _norm(text)
        if key in seen:
            continue
        seen.add(key)
        tips.append(text)
    return tips


def _split_first_sentence(text: str) -> tuple[str, str]:
    """Split on the first sentence-ending punctuation. Headline + remainder."""
    m = re.search(r"(?<=[.!?])\s+", text)
    if not m:
        return text.strip(), ""
    return text[: m.start()].strip(), text[m.end() :].strip()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()
