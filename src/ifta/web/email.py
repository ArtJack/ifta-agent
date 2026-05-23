"""Resend email integration for the web intake flow.

Three transactional emails:
- confirmation: link the customer clicks to start processing
- packet:       outputs as attachments after the worker finishes
- failure:      apology email when the pipeline can't produce a packet

In dev / test (no RESEND_API_KEY), every send is a no-op that returns False.
Submission flow falls back to "no confirmation" — `/submit` creates rows
directly in QUEUED instead of PENDING_CONFIRMATION.

Tests monkeypatch `_send_via_resend` to inspect calls without hitting the API.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ifta.web.models import Submission

log = logging.getLogger("ifta.web.email")


DEFAULT_FROM = "IFTA Service <ifta@artjeck.com>"
# /confirm/<token> lives on the FastAPI backend, not the marketing site —
# the default must match where deploy/README.md routes the Cloudflare Tunnel.
DEFAULT_PUBLIC_BASE_URL = "https://ifta-api.artjeck.com"


@dataclass(frozen=True)
class EmailConfig:
    api_key: str | None
    from_email: str = DEFAULT_FROM
    public_base_url: str = DEFAULT_PUBLIC_BASE_URL
    admin_bcc: tuple[str, ...] = field(default_factory=tuple)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


def load_email_config_from_env() -> EmailConfig:
    raw_bcc = os.environ.get("IFTA_WEB_ADMIN_BCC", "")
    admin_bcc = tuple(b.strip() for b in raw_bcc.split(",") if b.strip())
    return EmailConfig(
        api_key=os.environ.get("RESEND_API_KEY") or None,
        from_email=os.environ.get("RESEND_FROM_EMAIL") or DEFAULT_FROM,
        public_base_url=(
            os.environ.get("IFTA_WEB_PUBLIC_BASE_URL") or DEFAULT_PUBLIC_BASE_URL
        ).rstrip("/"),
        admin_bcc=admin_bcc,
    )


def _send_via_resend(params: dict[str, Any]) -> str | None:
    """Single point of contact with the Resend SDK. Tests monkeypatch this."""
    import resend

    # resend's SDK types params as a TypedDict; we build a regular dict to keep
    # the call site simple. The runtime call is identical.
    response = resend.Emails.send(params)  # type: ignore[arg-type]
    if isinstance(response, dict):
        return response.get("id")
    return getattr(response, "id", None)


class EmailClient:
    def __init__(self, config: EmailConfig) -> None:
        self.config = config
        if config.api_key:
            import resend

            resend.api_key = config.api_key

    # ─── public API ──────────────────────────────────────────────────────

    def send_confirmation(self, sub: Submission) -> bool:
        if not self.config.enabled:
            log.info("email disabled — skipping confirmation for %s", sub.id)
            return False
        confirm_url = f"{self.config.public_base_url}/confirm/{sub.confirm_token}"
        text = _confirmation_text(sub, confirm_url)
        return self._send(
            to=sub.email,
            subject=f"Confirm your {sub.quarter} IFTA submission",
            text=text,
        )

    def send_packet(self, sub: Submission, out_dir: Path) -> bool:
        if not self.config.enabled:
            log.info("email disabled — skipping packet for %s", sub.id)
            return False
        # Customer-facing artifacts only — the dev/audit files (review_note.md,
        # findings.json, customer_note.md itself) stay on the operator side.
        files = sorted(_collect_customer_attachments(out_dir))
        body = _read_customer_note(out_dir)
        text = _packet_text(sub, body)
        attachments = [_to_attachment(p) for p in files]
        return self._send(
            to=sub.email,
            subject=f"Your {sub.quarter} IFTA packet",
            text=text,
            attachments=attachments,
        )

    def send_failure(self, sub: Submission, error: str) -> bool:
        if not self.config.enabled:
            log.info("email disabled — skipping failure email for %s", sub.id)
            return False
        text = _failure_text(sub, error)
        return self._send(
            to=sub.email,
            subject=f"Couldn't process your {sub.quarter} IFTA submission",
            text=text,
        )

    def send_acknowledgement(self, sub: Submission) -> bool:
        """Sent immediately after upload -- tells the customer an operator will review."""
        if not self.config.enabled:
            log.info("email disabled — skipping acknowledgement for %s", sub.id)
            return False
        text = _acknowledgement_text(sub)
        return self._send(
            to=sub.email,
            subject=f"We received your {sub.quarter} IFTA files",
            text=text,
        )

    def send_rejection(self, sub: Submission, reason: str) -> bool:
        """Sent when an operator declines the submission."""
        if not self.config.enabled:
            log.info("email disabled — skipping rejection email for %s", sub.id)
            return False
        text = _rejection_text(sub, reason)
        return self._send(
            to=sub.email,
            subject=f"Your {sub.quarter} IFTA submission was not accepted",
            text=text,
        )

    # ─── internals ───────────────────────────────────────────────────────

    def _send(
        self,
        *,
        to: str,
        subject: str,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> bool:
        params: dict[str, Any] = {
            "from": self.config.from_email,
            "to": [to],
            "subject": subject,
            "text": text,
        }
        if attachments:
            params["attachments"] = attachments
        if self.config.admin_bcc:
            params["bcc"] = list(self.config.admin_bcc)
        try:
            email_id = _send_via_resend(params)
            log.info("sent %r to %s (id=%s)", subject, to, email_id)
            return True
        except Exception:
            log.exception("failed to send %r to %s", subject, to)
            return False


# ─── helpers ──────────────────────────────────────────────────────────────


def _collect_packet_files(out_dir: Path) -> Iterable[Path]:
    if not out_dir.exists():
        return []
    files: list[Path] = []
    for p in out_dir.rglob("*"):
        if p.is_file():
            files.append(p)
    return files


# Files the *customer* actually needs in their inbox. We deliberately exclude
# review_note.md and findings.json (dev/audit artifacts) and customer_note.md
# (already inlined into the email body — no point re-attaching).
_CUSTOMER_EXCLUDED_NAMES = {"review_note.md", "findings.json", "customer_note.md"}


def _collect_customer_attachments(out_dir: Path) -> Iterable[Path]:
    if not out_dir.exists():
        return []
    files: list[Path] = []
    # Portal CSV at the top level.
    portal = out_dir / "ifta_portal.csv"
    if portal.exists():
        files.append(portal)
    # Detailed customer summary report (plain English; problems, why-it-matters,
    # what-to-do) — gives the customer/accountant a self-contained record while
    # keeping the email body itself minimal.
    summary = out_dir / "summary_report.md"
    if summary.exists():
        files.append(summary)
    # One Excel per truck (or any other intentionally-attached *.xlsx).
    for p in sorted(out_dir.rglob("*.xlsx")):
        if p.name not in _CUSTOMER_EXCLUDED_NAMES:
            files.append(p)
    return files


def _to_attachment(path: Path) -> dict[str, Any]:
    return {"filename": path.name, "content": list(path.read_bytes())}


def _read_customer_note(out_dir: Path) -> str:
    """Read the friendly customer email body. Falls back to review_note.md
    only if the customer note wasn't produced (e.g. running against an older
    submission's output dir from before this feature shipped)."""
    candidates = list(out_dir.rglob("customer_note.md"))
    if not candidates:
        candidates = list(out_dir.rglob("review_note.md"))
    if not candidates:
        return ""
    return candidates[0].read_text(encoding="utf-8")


def _confirmation_text(sub: Submission, confirm_url: str) -> str:
    company_line = f" for {sub.company}" if sub.company else ""
    return (
        f"Hi,\n\n"
        f"You uploaded mileage and fuel files for {sub.quarter}{company_line} "
        f"to ArtJeck's IFTA service.\n\n"
        f"Click the link below to start processing. Your packet will arrive at "
        f"this address in about 5 minutes:\n\n"
        f"  {confirm_url}\n\n"
        f"If you didn't make this request, ignore this email — nothing happens "
        f"without that click.\n\n"
        f"— ArtJeck IFTA\n"
    )


def _packet_text(sub: Submission, customer_note: str) -> str:
    """Compose the packet email body.

    The customer note (built by ifta.web.customer_view) is plain English and
    self-contained — it already greets the customer, gives the headline status
    + key totals, lists 'before you file' items, lists attachments, and signs
    off. We just hand it through. Falls back to a minimal default if the note
    is missing (older submissions, or a renderer bug).
    """
    body = (customer_note or "").strip()
    if body:
        return body + "\n"
    # Defensive fallback so the email always says something useful even if
    # render_customer_view returned an empty string.
    company_line = f" for {sub.company}" if sub.company else ""
    greeting = f"Hi {sub.name}," if sub.name else "Hi,"
    return (
        f"{greeting}\n\n"
        f"Your {sub.quarter} IFTA packet{company_line} is attached.\n\n"
        f"• ifta_portal.csv — upload this directly to your state's IFTA portal\n"
        f"• trucks/<id>.xlsx — per-truck breakdown\n\n"
        f"Questions? Just reply — Eugene reads every email.\n\n"
        f"— ArtJeck IFTA\n"
    )


def _failure_text(sub: Submission, error: str) -> str:
    return (
        f"Hi,\n\n"
        f"We hit an issue processing your {sub.quarter} files:\n\n"
        f"{error.strip()}\n\n"
        f"Reply to this email and Eugene will take a look — usually the fix is "
        f"a quick re-upload with the right file format.\n\n"
        f"— ArtJeck IFTA\n"
    )


def _acknowledgement_text(sub: Submission) -> str:
    company_line = f" for {sub.company}" if sub.company else ""
    name_greeting = f"Hi {sub.name},\n\n" if sub.name else "Hi,\n\n"
    return (
        f"{name_greeting}"
        f"We received your mileage and fuel files for {sub.quarter}{company_line}.\n\n"
        f"An operator will review your submission shortly. Once approved, your "
        f"IFTA packet will be processed and sent to this address.\n\n"
        f"If you have questions, reply to this email.\n\n"
        f"— ArtJeck IFTA\n"
    )


def _rejection_text(sub: Submission, reason: str) -> str:
    name_greeting = f"Hi {sub.name},\n\n" if sub.name else "Hi,\n\n"
    return (
        f"{name_greeting}"
        f"We reviewed your {sub.quarter} IFTA submission and were unable to "
        f"accept it at this time.\n\n"
        f"Reason: {reason.strip()}\n\n"
        f"You're welcome to re-submit at artjeck.com/ifta once the issue is "
        f"resolved. Reply to this email if you have questions.\n\n"
        f"— ArtJeck IFTA\n"
    )
