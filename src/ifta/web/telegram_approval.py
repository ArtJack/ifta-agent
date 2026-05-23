"""Send Telegram approval cards with Accept/Decline inline buttons.

Uses direct ``requests.post`` calls (same pattern as ``AdminNotifier``).
The web app sends the card; the existing ``ifta telegram-bot`` process
handles the callback queries when an operator taps a button.

Callback data format (must stay under 64 bytes):
    ``wa:<submission_id>``   -- Accept (run the pipeline)
    ``wd:<submission_id>``   -- Decline (final reject)
    ``wm:<submission_id>``   -- Request more files from the customer
"""

from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import dataclass

import requests

from ifta.web.models import Submission

log = logging.getLogger("ifta.web.telegram_approval")

TELEGRAM_API = "https://api.telegram.org/bot{token}"

# Callback data prefixes -- keep short (Telegram 64-byte limit).
CB_WEB_ACCEPT = "wa"
CB_WEB_DECLINE = "wd"
CB_WEB_MORE_FILES = "wm"


@dataclass(frozen=True)
class TelegramApprovalConfig:
    token: str | None
    chat_ids: tuple[int, ...]

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_ids)


def load_approval_config() -> TelegramApprovalConfig:
    """Read config from the same env vars as ``AdminNotifier``."""
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip() or None
    raw = os.environ.get("TELEGRAM_ADMIN_CHAT_ID") or os.environ.get(
        "TELEGRAM_ADMIN_USER_IDS"
    )
    chat_ids: tuple[int, ...] = ()
    if raw:
        ids: list[int] = []
        for chunk in re.split(r"[,\s]+", raw.strip()):
            if not chunk:
                continue
            try:
                ids.append(int(chunk))
            except ValueError:
                log.warning("ignoring non-numeric admin chat id %r", chunk)
        chat_ids = tuple(ids)
    return TelegramApprovalConfig(token=token, chat_ids=chat_ids)


class TelegramApprovalClient:
    """Send and edit Telegram approval cards for web submissions."""

    def __init__(
        self, config: TelegramApprovalConfig, *, timeout: float = 10.0
    ) -> None:
        self.config = config
        self.timeout = timeout

    # ------------------------------------------------------------------ send

    def send_approval_card(
        self, sub: Submission, summary: str
    ) -> list[tuple[int, int]]:
        """Send an HTML message with Accept/Decline buttons to every admin chat.

        Returns a list of (chat_id, message_id) pairs for each successfully
        sent message.
        """
        if not self.config.enabled:
            log.debug("telegram approval disabled -- skipping card for %s", sub.id)
            return []

        text = _build_card_html(sub, summary)
        reply_markup = _build_inline_keyboard(sub.id)
        results: list[tuple[int, int]] = []

        for chat_id in self.config.chat_ids:
            message_id = self._send_message(chat_id, text, reply_markup)
            if message_id is not None:
                results.append((chat_id, message_id))
        return results

    # ------------------------------------------------------------------ edit

    def edit_card_approved(
        self,
        chat_id: int,
        message_id: int,
        sub: Submission,
        decided_by: str,
    ) -> bool:
        """Edit the card in place to show the approval decision."""
        company = html.escape(sub.company or sub.email)
        text = (
            f"<b>IFTA submission approved</b>\n\n"
            f"<b>Customer:</b> {company}\n"
            f"<b>Quarter:</b> {html.escape(sub.quarter)}\n"
            f"<b>Submission:</b> <code>{sub.id[:12]}</code>\n\n"
            f"Approved by {html.escape(decided_by)}."
        )
        return self._edit_message(chat_id, message_id, text)

    def edit_card_more_files_requested(
        self,
        chat_id: int,
        message_id: int,
        sub: Submission,
        decided_by: str,
    ) -> bool:
        """Edit the card in place to show 'more files requested from customer'.

        Removes the inline keyboard so the operator can't double-tap; if the
        customer later re-uploads via artjeck.com they'll get a fresh approval
        card for the new submission.
        """
        company = html.escape(sub.company or sub.email)
        text = (
            f"<b>📩 More files requested from customer</b>\n\n"
            f"<b>Customer:</b> {company}\n"
            f"<b>Quarter:</b> {html.escape(sub.quarter)}\n"
            f"<b>Submission:</b> <code>{sub.id[:12]}</code>\n\n"
            f"Requested by {html.escape(decided_by)} -- "
            f"the customer has been emailed a plain-English ask for the missing files."
        )
        return self._edit_message(chat_id, message_id, text)

    def edit_card_rejected(
        self,
        chat_id: int,
        message_id: int,
        sub: Submission,
        decided_by: str,
        reason: str,
    ) -> bool:
        """Edit the card in place to show the rejection decision."""
        company = html.escape(sub.company or sub.email)
        text = (
            f"<b>IFTA submission declined</b>\n\n"
            f"<b>Customer:</b> {company}\n"
            f"<b>Quarter:</b> {html.escape(sub.quarter)}\n"
            f"<b>Submission:</b> <code>{sub.id[:12]}</code>\n\n"
            f"Declined by {html.escape(decided_by)}.\n"
            f"Reason: {html.escape(reason)}"
        )
        return self._edit_message(chat_id, message_id, text)

    # -------------------------------------------------------------- internals

    def _send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> int | None:
        """Send an HTML message and return the message_id, or None on failure."""
        if not self.config.token:
            return None
        url = f"{TELEGRAM_API.format(token=self.config.token)}/sendMessage"
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            r = requests.post(url, json=payload, timeout=self.timeout)
            if r.status_code != 200:
                log.warning(
                    "approval card to %s failed: HTTP %s -- %s",
                    chat_id,
                    r.status_code,
                    r.text[:200],
                )
                return None
            data = r.json()
            return data.get("result", {}).get("message_id")
        except requests.RequestException as e:
            log.warning("approval card to %s raised: %s", chat_id, e)
            return None

    def _edit_message(
        self, chat_id: int, message_id: int, text: str
    ) -> bool:
        """Edit an existing message (remove inline keyboard)."""
        if not self.config.token:
            return False
        url = f"{TELEGRAM_API.format(token=self.config.token)}/editMessageText"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            r = requests.post(url, json=payload, timeout=self.timeout)
            if r.status_code != 200:
                log.warning(
                    "edit card %s/%s failed: HTTP %s -- %s",
                    chat_id,
                    message_id,
                    r.status_code,
                    r.text[:200],
                )
                return False
            return True
        except requests.RequestException as e:
            log.warning("edit card %s/%s raised: %s", chat_id, message_id, e)
            return False


# ---- message builders -------------------------------------------------------


def _build_card_html(sub: Submission, summary: str) -> str:
    """Build the HTML body for the approval card."""
    name = html.escape(sub.name or "(not provided)")
    email = html.escape(sub.email)
    company = html.escape(sub.company or "(not provided)")
    quarter = html.escape(sub.quarter)
    base_state = html.escape(sub.base_state or "--")
    fleet_size = str(sub.fleet_size) if sub.fleet_size else "--"
    sid_short = sub.id[:12]

    lines = [
        "<b>New IFTA submission -- review required</b>",
        "",
        f"<b>Name:</b> {name}",
        f"<b>Email:</b> {email}",
        f"<b>Company:</b> {company}",
        f"<b>Quarter:</b> {quarter}",
        f"<b>Base state:</b> {base_state}",
        f"<b>Fleet size:</b> {fleet_size}",
        f"<b>Submission:</b> <code>{sid_short}</code>",
    ]
    if sub.notes:
        lines += ["", f"<b>Notes:</b> {html.escape(sub.notes[:500])}"]
    lines += ["", html.escape(summary)]
    return "\n".join(lines)


def _build_inline_keyboard(submission_id: str) -> dict:
    """Build the Telegram inline keyboard JSON for Accept / Decline / More files.

    Three buttons on two rows to keep the labels readable on phone screens:
    Accept + Decline on the top row (the most common decisions), and the
    softer "Request more files" path below.
    """
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Accept",
                    "callback_data": f"{CB_WEB_ACCEPT}:{submission_id}",
                },
                {
                    "text": "❌ Decline",
                    "callback_data": f"{CB_WEB_DECLINE}:{submission_id}",
                },
            ],
            [
                {
                    "text": "📩 Request more files",
                    "callback_data": f"{CB_WEB_MORE_FILES}:{submission_id}",
                },
            ],
        ]
    }
