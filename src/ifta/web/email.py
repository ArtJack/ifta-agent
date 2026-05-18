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
        files = sorted(_collect_packet_files(out_dir))
        review_note = _read_review_note(out_dir)
        text = _packet_text(sub, review_note)
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


def _to_attachment(path: Path) -> dict[str, Any]:
    return {"filename": path.name, "content": list(path.read_bytes())}


def _read_review_note(out_dir: Path) -> str:
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


def _packet_text(sub: Submission, review_note: str) -> str:
    company_line = f"\nCarrier: {sub.company}" if sub.company else ""
    note = review_note.strip()
    return (
        f"Hi,\n\n"
        f"Your {sub.quarter} IFTA packet is attached.{company_line}\n\n"
        f"Attached files:\n"
        f"  • ifta_portal.csv — upload this directly to your state portal\n"
        f"  • review_note.md  — preflight findings + summary (below)\n"
        f"  • trucks/*.xlsx   — one per truck (forward to each owner-operator)\n\n"
        f"───── Review note ─────\n\n"
        f"{note}\n\n"
        f"Questions? Reply to this email — Eugene reads them.\n\n"
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
