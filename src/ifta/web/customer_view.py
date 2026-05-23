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
CUSTOMER_SUMMARY_FILENAME = "summary_report.pdf"
CUSTOMER_SUMMARY_PDF_FILENAME = "summary_report.pdf"

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
    lines.append("• summary_report.pdf — full plain-English breakdown for your records")
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


# ─── customer-facing failure email ────────────────────────────────────────────


def render_customer_failure(*, sub: Submission, error: str) -> str:
    """Short, friendly body for the failure email.

    The raw `error` string (from PipelineError or an unexpected exception) is
    a developer dump — `[CODE] message`, "ERRORS (1):" headers, `file_` prefixed
    filenames, "--force" jargon. Strip all of it and present the customer a
    plain-English explanation + a clear "what to do next" path.
    """
    greeting = f"Hi {sub.name}," if sub.name else "Hi,"
    bullets = _humanize_error_lines(error)
    lines: list[str] = [greeting, ""]
    lines.append(
        f"We received your {sub.quarter} files, but we couldn't finish your packet yet — "
        f"a couple of things need a closer look before we can file:"
    )
    lines.append("")
    if bullets:
        for b in bullets:
            lines.append(f"• {b}")
    else:
        # Unparseable error string — fall back to a generic friendly message.
        lines.append("• Something on our end didn't quite work with the files you sent.")
    lines.append("")
    lines.append("What to do next:")
    lines.append("• Reply to this email with the missing or corrected files — Eugene reads every one.")
    lines.append("• Or re-upload at https://artjeck.com/ifta/submit")
    lines.append("")
    lines.append("Your files are safe on our end. We'll pick up the moment we hear back.")
    lines.append("")
    lines.append("Attached: summary_report.pdf — full plain-English breakdown for your records.")
    lines.append("")
    lines.append("— ArtJeck IFTA")
    return "\n".join(lines) + "\n"


def render_customer_failure_report(*, sub: Submission, error: str) -> str:
    """Detailed plain-English failure report (attached file).

    Long-form companion to `render_customer_failure` for customers and their
    accountants. Same anti-leakage rules: no codes, no JSON, no "[ERROR]"
    prefixes, no `file_` underscore-mangled names.
    """
    carrier = sub.company or "your company"
    errors, warnings = _humanize_error_sections(error)

    lines: list[str] = [
        f"# IFTA {sub.quarter} — Couldn't Finish Yet ({carrier})",
        "",
        f"_Prepared for {sub.email}._",
        "",
        f"We received your files for {sub.quarter}, but we hit a couple of issues "
        f"that need your input before we can compute your filing. Nothing is lost — "
        f"your files are saved with us, and we'll pick up the moment you reply with "
        f"the missing pieces.",
        "",
        "## What we noticed",
        "",
    ]
    if errors:
        for headline, detail in errors:
            lines.append(f"### {headline}")
            lines.append("")
            if detail:
                lines.append(detail)
                lines.append("")
    else:
        lines.append(
            "Something on our end didn't quite work with the files you sent. "
            "Reply to your packet email and Eugene will dig in."
        )
        lines.append("")

    if warnings:
        lines.append("## Also worth a look")
        lines.append("")
        for headline, detail in warnings:
            text = headline if not detail else f"{headline} {detail}"
            lines.append(f"- {text}")
        lines.append("")

    lines.append("## What to do next")
    lines.append("")
    lines.append("- Reply to your packet email with the missing or corrected files.")
    lines.append("- Or re-upload everything at https://artjeck.com/ifta/submit")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("Questions? Reply to the email this report came with — Eugene reads every one.")
    lines.append("")
    return "\n".join(lines) + "\n"


# Headers and noise we want stripped from the raw error dump.
_NOISE_HEADERS = (
    "preflight found error-level issues in your uploaded files:",
    "preflight found warning-level issues in your uploaded files:",
)
_SECTION_HEADER = re.compile(r"^\s*(ERRORS|WARNINGS|INFOS)\s*\(\s*\d+\s*\)\s*:?\s*$", re.IGNORECASE)
_BULLET = re.compile(r"^\s*[-•]?\s*\[([A-Z][A-Z0-9_]*)\]\s+(.+)$")
# Presence-only check, used to decide whether the input looks like a preflight
# dump. Distinct from _BULLET (which is line-anchored) because the structured
# format guard runs against the whole multi-line string.
_HAS_CODE_BULLET = re.compile(r"\[[A-Z][A-Z0-9_]+\]")
_FILE_PREFIX = re.compile(r"\bfile_([A-Za-z0-9_.\-]+)")
_FORCE_JARGON = re.compile(
    r"\s*(?:Remove one source or )?re-?run with --force only after manual confirmation\.?",
    re.IGNORECASE,
)


def _humanize_error_lines(error: str) -> list[str]:
    """Flatten the raw preflight error dump into plain-English bullets.

    Only returns bullets when the input looks like the structured preflight
    format (recognisable `[CODE] ...` lines). Anything else — a raw Python
    exception, a stack trace, an opaque "boom" — returns [] so the caller
    can fall back to a single generic friendly bullet instead of leaking
    developer output to the customer.
    """
    if not _HAS_CODE_BULLET.search(error):
        return []
    bullets: list[str] = []
    for raw in error.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower() in _NOISE_HEADERS:
            continue
        if _SECTION_HEADER.match(line):
            continue
        m = _BULLET.match(line)
        if m:
            bullets.append(_clean_finding_text(m.group(2)))
        elif bullets:
            # Continuation line from a wrapped message — append to the most
            # recent bullet rather than starting a new one.
            bullets[-1] = (bullets[-1] + " " + _clean_finding_text(line)).strip()
        # Free-floating lines before the first [CODE] bullet are dropped.
    return [b for b in bullets if b]


def _humanize_error_sections(error: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Same parse, but partitioned into (errors, warnings) and split into
    headline + detail for the detailed report's section layout. Bails to
    empty lists when the input doesn't carry the structured preflight format,
    so opaque Python exceptions never reach the customer as bare text."""
    if not _HAS_CODE_BULLET.search(error):
        return [], []
    errors: list[tuple[str, str]] = []
    warnings: list[tuple[str, str]] = []
    current = errors
    for raw in error.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower() in _NOISE_HEADERS:
            continue
        sm = _SECTION_HEADER.match(line)
        if sm:
            kind = sm.group(1).upper()
            current = warnings if kind == "WARNINGS" else errors
            continue
        m = _BULLET.match(line)
        if m:
            text = _clean_finding_text(m.group(2))
            headline, detail = _split_first_sentence(text)
            current.append((headline, detail))
        # Lines that aren't [CODE] bullets are dropped — by this point we know
        # the input has at least one [CODE] bullet (guarded above), so stray
        # non-bullet lines are noise like the "Preflight found..." header.
    return errors, warnings


def _clean_finding_text(text: str) -> str:
    """Strip developer markup from a single finding-message string."""
    out = text
    # Drop the leading [CODE] if a continuation line still has it.
    out = re.sub(r"^\s*\[[A-Z][A-Z0-9_]*\]\s*", "", out)
    # Restore filenames: file_ifta-DM_EXPRESS_INC.xlsx → "ifta-DM EXPRESS INC.xlsx".
    out = _FILE_PREFIX.sub(lambda m: m.group(1).replace("_", " "), out)
    # Drop dev jargon about --force.
    out = _FORCE_JARGON.sub("", out)
    # Collapse whitespace + tidy punctuation.
    out = re.sub(r"\s+", " ", out).strip()
    if out and not out.endswith((".", "!", "?")):
        out += "."
    return out


# ─── PDF renderers ────────────────────────────────────────────────────────────
#
# Customers expect a real document they can open in Preview, print, sign, and
# archive — a .md file looks like developer output. These renderers consume
# the same `_structured_problems` / `_structured_tips` / `_humanize_error_sections`
# helpers as the markdown ones, so the two formats stay in sync.


def render_customer_summary_pdf(
    *,
    sub: Submission,
    ret: IftaReturn,
    note: Any | None = None,
    findings: list[Finding] | None = None,
    truck_count: int = 0,
    attached_files: list[str] | None = None,
) -> bytes:
    """PDF companion to render_customer_summary — same content, designed for
    the customer's email attachment."""
    flow = _build_summary_flowables(
        sub=sub,
        ret=ret,
        note=note,
        findings=findings,
        truck_count=truck_count,
        attached_files=attached_files,
    )
    return _build_pdf(title=f"IFTA {sub.quarter} Summary — {sub.company or 'your company'}", flow=flow)


def render_customer_failure_report_pdf(*, sub: Submission, error: str) -> bytes:
    """PDF companion to render_customer_failure_report."""
    flow = _build_failure_flowables(sub=sub, error=error)
    return _build_pdf(
        title=f"IFTA {sub.quarter} — Couldn't Finish Yet ({sub.company or 'your company'})",
        flow=flow,
    )


def _build_pdf(*, title: str, flow: list[Any]) -> bytes:
    """Render a list of reportlab flowables to a PDF, returned as raw bytes."""
    # Local imports — keep the heavy PDF stack out of import paths that don't
    # need it (tests, the CLI 'run' subcommand, etc.).
    from io import BytesIO

    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        title=title,
        author="ArtJeck IFTA",
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )
    doc.build(flow)
    return buf.getvalue()


def _pdf_styles() -> dict[str, Any]:
    """Lazy-load reportlab styles so importing this module is cheap."""
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "Title", parent=base["Title"], fontSize=18, spaceAfter=4, textColor=colors.HexColor("#0a2540")
        ),
        "byline": ParagraphStyle(
            "Byline",
            parent=base["BodyText"],
            fontSize=10,
            textColor=colors.HexColor("#5b6b7c"),
            spaceAfter=14,
        ),
        "status": ParagraphStyle(
            "Status",
            parent=base["Heading2"],
            fontSize=13,
            textColor=colors.HexColor("#0a8a3a"),
            spaceBefore=2,
            spaceAfter=12,
        ),
        "h2": ParagraphStyle(
            "H2",
            parent=base["Heading2"],
            fontSize=12,
            textColor=colors.HexColor("#0a2540"),
            spaceBefore=12,
            spaceAfter=6,
        ),
        "h3": ParagraphStyle(
            "H3",
            parent=base["Heading3"],
            fontSize=11,
            textColor=colors.HexColor("#1a3a5a"),
            spaceBefore=8,
            spaceAfter=3,
        ),
        "body": ParagraphStyle(
            "Body", parent=base["BodyText"], fontSize=10.5, leading=14, spaceAfter=4
        ),
        "small": ParagraphStyle(
            "Small",
            parent=base["BodyText"],
            fontSize=9.5,
            leading=12,
            textColor=colors.HexColor("#5b6b7c"),
            spaceAfter=2,
        ),
        "bullet": ParagraphStyle(
            "Bullet",
            parent=base["BodyText"],
            fontSize=10.5,
            leading=14,
            leftIndent=14,
            firstLineIndent=-10,
            spaceAfter=3,
        ),
    }


def _build_summary_flowables(
    *,
    sub: Submission,
    ret: IftaReturn,
    note: Any | None,
    findings: list[Finding] | None,
    truck_count: int,
    attached_files: list[str] | None,
) -> list[Any]:
    from reportlab.platypus import Paragraph, Spacer

    s = _pdf_styles()
    carrier = sub.company or "your company"
    status_text = _FILING_STATUS_HEADER.get(
        getattr(note, "filing_status", "") or "", "Status: Packet ready"
    )

    flow: list[Any] = [
        Paragraph(f"IFTA {sub.quarter} Summary Report — {_pdf_escape(carrier)}", s["title"]),
        Paragraph(f"Prepared for {_pdf_escape(sub.email)}", s["byline"]),
        Paragraph(_pdf_escape(status_text), s["status"]),
        Paragraph("Key numbers", s["h2"]),
        Paragraph(
            f"<b>Total tax due:</b> ${ret.total_tax_due:,.2f}<br/>"
            f"<b>Fleet MPG:</b> {ret.fleet_mpg:.2f} "
            f"<font color='#5b6b7c'>(realistic range for heavy trucks is roughly 5–8)</font><br/>"
            f"<b>Fleet miles:</b> {ret.fleet_miles:,.0f}<br/>"
            f"<b>Fleet gallons:</b> {ret.fleet_gallons:,.2f}"
            + (f"<br/><b>Trucks on this return:</b> {truck_count}" if truck_count else ""),
            s["body"],
        ),
    ]

    if ret.rate_warning:
        flow.append(Paragraph("⚠️ Rate notice", s["h2"]))
        flow.append(Paragraph(_pdf_escape(ret.rate_warning), s["body"]))

    problems = _structured_problems(note=note, findings=findings)
    if problems:
        flow.append(Paragraph("Things to double-check before filing", s["h2"]))
        for p in problems:
            flow.append(Paragraph(_pdf_escape(p["headline"]), s["h3"]))
            if p["detail"]:
                flow.append(Paragraph(_pdf_escape(p["detail"]), s["body"]))
            if p["why"]:
                flow.append(Paragraph(f"<b>Why it matters:</b> {_pdf_escape(p['why'])}", s["body"]))
            if p["what_to_do"]:
                flow.append(Paragraph(f"<b>What to do:</b> {_pdf_escape(p['what_to_do'])}", s["body"]))

    tips = _structured_tips(note=note)
    if tips:
        flow.append(Paragraph("Filing tips", s["h2"]))
        for t in tips:
            flow.append(Paragraph(f"• {_pdf_escape(t)}", s["bullet"]))

    if attached_files:
        flow.append(Paragraph("Files in this packet", s["h2"]))
        for name in attached_files:
            flow.append(Paragraph(f"• {_pdf_escape(name)}", s["bullet"]))

    flow.append(Spacer(1, 12))
    flow.append(
        Paragraph(
            "Questions? Reply to the email this report came with — Eugene reads every one.",
            s["small"],
        )
    )
    return flow


def _build_failure_flowables(*, sub: Submission, error: str) -> list[Any]:
    from reportlab.platypus import Paragraph, Spacer

    s = _pdf_styles()
    carrier = sub.company or "your company"
    errors, warnings = _humanize_error_sections(error)

    flow: list[Any] = [
        Paragraph(
            f"IFTA {sub.quarter} — Couldn't Finish Yet ({_pdf_escape(carrier)})", s["title"]
        ),
        Paragraph(f"Prepared for {_pdf_escape(sub.email)}", s["byline"]),
        Paragraph(
            "We received your files for "
            f"{_pdf_escape(sub.quarter)}, but we hit a couple of issues "
            "that need your input before we can compute your filing. "
            "Nothing is lost — your files are saved with us, and we'll "
            "pick up the moment you reply with the missing pieces.",
            s["body"],
        ),
        Paragraph("What we noticed", s["h2"]),
    ]
    if errors:
        for headline, detail in errors:
            flow.append(Paragraph(_pdf_escape(headline), s["h3"]))
            if detail:
                flow.append(Paragraph(_pdf_escape(detail), s["body"]))
    else:
        flow.append(
            Paragraph(
                "Something on our end didn't quite work with the files you sent. "
                "Reply to your packet email and Eugene will dig in.",
                s["body"],
            )
        )

    if warnings:
        flow.append(Paragraph("Also worth a look", s["h2"]))
        for headline, detail in warnings:
            text = headline if not detail else f"{headline} {detail}"
            flow.append(Paragraph(f"• {_pdf_escape(text)}", s["bullet"]))

    flow.append(Paragraph("What to do next", s["h2"]))
    flow.append(Paragraph("• Reply to your packet email with the missing or corrected files.", s["bullet"]))
    flow.append(
        Paragraph(
            "• Or re-upload everything at "
            "<link href='https://artjeck.com/ifta/submit' color='#0066cc'>"
            "https://artjeck.com/ifta/submit</link>",
            s["bullet"],
        )
    )
    flow.append(Spacer(1, 12))
    flow.append(
        Paragraph(
            "Questions? Reply to the email this report came with — Eugene reads every one.",
            s["small"],
        )
    )
    return flow


# ─── "Request more files" renderers ──────────────────────────────────────────
#
# Reused for the operator's "📩 Request more files" Telegram button (Step 8
# slice 3). The framing is slightly softer than the rejection path — we're
# not telling the customer "we couldn't process this", we're telling them
# "we're ready to file, we just need a bit more first". Same anti-leakage
# guarantees as the failure renderers; same helpers do the dev-markup
# stripping and the structured parse.


def render_more_files_request(*, sub: Submission, intake_brief: str) -> str:
    """Short, friendly email body for the operator's 'more files' request."""
    greeting = f"Hi {sub.name}," if sub.name else "Hi,"
    bullets = _humanize_intake_brief_lines(intake_brief)
    lines: list[str] = [greeting, ""]
    lines.append(
        f"We received your {sub.quarter} files — thanks. Before we can finish "
        "your filing we'd like a bit more from you:"
    )
    lines.append("")
    if bullets:
        for b in bullets:
            lines.append(f"• {b}")
    else:
        lines.append(
            "• A couple of additional records would help us finish — Eugene will "
            "follow up with specifics when you reply."
        )
    lines.append("")
    lines.append("How to send what's missing:")
    lines.append("• Reply to this email with the missing files attached — Eugene reads every one.")
    lines.append("• Or re-upload everything at https://artjeck.com/ifta/submit")
    lines.append("")
    lines.append(
        "Your files are safe on our end — no need to re-send the same ones "
        "unless you want to. We'll pick up the moment we hear back."
    )
    lines.append("")
    lines.append("Attached: summary_report.pdf — the full plain-English breakdown for your records.")
    lines.append("")
    lines.append("— ArtJeck IFTA")
    return "\n".join(lines) + "\n"


def render_more_files_request_pdf(*, sub: Submission, intake_brief: str) -> bytes:
    """PDF companion to render_more_files_request — same content as the
    detailed customer report, framed as 'a few more pieces needed'."""
    flow = _build_more_files_flowables(sub=sub, intake_brief=intake_brief)
    return _build_pdf(
        title=f"IFTA {sub.quarter} — A few more files needed ({sub.company or 'your company'})",
        flow=flow,
    )


def _build_more_files_flowables(*, sub: Submission, intake_brief: str) -> list[Any]:
    from reportlab.platypus import Paragraph, Spacer

    s = _pdf_styles()
    carrier = sub.company or "your company"
    findings = _humanize_intake_brief_sections(intake_brief)

    flow: list[Any] = [
        Paragraph(
            f"IFTA {sub.quarter} — A few more files needed ({_pdf_escape(carrier)})",
            s["title"],
        ),
        Paragraph(f"Prepared for {_pdf_escape(sub.email)}", s["byline"]),
        Paragraph(
            f"We received your files for {_pdf_escape(sub.quarter)} — thanks. "
            "Before we can finish your filing we'd like a bit more from you. "
            "Nothing is lost; your files are saved on our end and we'll pick "
            "up the moment you reply.",
            s["body"],
        ),
        Paragraph("What we'd like to clarify", s["h2"]),
    ]
    if findings:
        for headline, detail in findings:
            flow.append(Paragraph(_pdf_escape(headline), s["h3"]))
            if detail:
                flow.append(Paragraph(_pdf_escape(detail), s["body"]))
    else:
        flow.append(
            Paragraph(
                "Eugene will follow up by email with specifics — reply to your "
                "packet email and we'll take it from there.",
                s["body"],
            )
        )

    flow.append(Paragraph("How to send what's missing", s["h2"]))
    flow.append(Paragraph("• Reply to your packet email with the missing files attached.", s["bullet"]))
    flow.append(
        Paragraph(
            "• Or re-upload everything at "
            "<link href='https://artjeck.com/ifta/submit' color='#0066cc'>"
            "https://artjeck.com/ifta/submit</link>",
            s["bullet"],
        )
    )
    flow.append(Spacer(1, 12))
    flow.append(
        Paragraph(
            "Questions? Reply to the email this report came with — Eugene reads every one.",
            s["small"],
        )
    )
    return flow


# The intake brief uses the `- [SEVERITY] CODE: message` format produced by
# ifta.web.intake_brief, distinct from preflight's `[CODE] message` dump that
# the failure renderers parse. The bullets, the dev-markup stripping, and the
# headline/detail split are the same — just a different leading shape.
_INTAKE_BULLET = re.compile(
    r"^\s*[-•]?\s*\[(?:ERROR|WARNING|INFO)\]\s+([A-Z][A-Z0-9_]*)\s*:\s*(.+)$",
    re.IGNORECASE,
)
# Presence-only counterpart — used against the whole multi-line brief, where
# the line-anchored _INTAKE_BULLET regex would fail without re.MULTILINE.
_HAS_INTAKE_BULLET = re.compile(
    r"\[(?:ERROR|WARNING|INFO)\]\s+[A-Z][A-Z0-9_]*\s*:", re.IGNORECASE
)


def _humanize_intake_brief_lines(brief: str) -> list[str]:
    """Pull warning/error bullets out of the intake brief as plain English.

    Returns [] if the input doesn't look like an intake brief so the caller
    can fall back to a generic friendly message rather than leaking opaque
    text to the customer.
    """
    if not _HAS_INTAKE_BULLET.search(brief):
        return []
    bullets: list[str] = []
    for raw in brief.splitlines():
        m = _INTAKE_BULLET.match(raw.strip())
        if not m:
            continue
        text = _clean_finding_text(m.group(2))
        if text:
            bullets.append(text)
    return bullets


def _humanize_intake_brief_sections(brief: str) -> list[tuple[str, str]]:
    """Same as _humanize_intake_brief_lines but split into (headline, detail)
    pairs for the detailed PDF layout."""
    out: list[tuple[str, str]] = []
    for line in _humanize_intake_brief_lines(brief):
        out.append(_split_first_sentence(line))
    return out


def _pdf_escape(text: str) -> str:
    """Escape text for reportlab's mini-HTML Paragraph dialect.

    Paragraph treats `<`, `>`, `&` as markup. We don't want stray characters
    in customer-supplied names (e.g. ampersands in carrier names) to crash the
    PDF build, and we never inject untrusted markup ourselves — only the
    explicit tags we build into the template (b, br, font, link).
    """
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
