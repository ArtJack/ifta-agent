"""Send admin-only Telegram notifications when customers use the IFTA service.

A separate, fire-and-forget channel from the customer-facing Telegram intake
bot. Reuses `TELEGRAM_BOT_TOKEN`, but routes messages to chat IDs in the new
`TELEGRAM_ADMIN_CHAT_ID` env var (falls back to `TELEGRAM_ADMIN_USER_IDS` so a
single admin can configure both without duplication).

Failures here must never break the customer flow — every public call swallows
exceptions and logs them.
"""

from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger("ifta.notify")

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
# Telegram caps a single sendMessage payload at 4096 chars including formatting.
# Reserve some headroom for the wrapping HTML.
TELEGRAM_MAX_BODY = 3800


@dataclass(frozen=True)
class AdminNotifierConfig:
    token: str | None
    chat_ids: tuple[int, ...]

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_ids)


def _parse_chat_ids(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    out: list[int] = []
    for chunk in re.split(r"[,\s]+", value.strip()):
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError:
            log.warning("ignoring non-numeric admin chat id %r", chunk)
    return tuple(out)


def load_admin_notifier_config() -> AdminNotifierConfig:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip() or None
    chat_ids = _parse_chat_ids(os.environ.get("TELEGRAM_ADMIN_CHAT_ID"))
    if not chat_ids:
        chat_ids = _parse_chat_ids(os.environ.get("TELEGRAM_ADMIN_USER_IDS"))
    return AdminNotifierConfig(token=token, chat_ids=chat_ids)


class AdminNotifier:
    """Thin wrapper around Telegram's sendMessage endpoint.

    Disabled (no-op) when `TELEGRAM_BOT_TOKEN` or `TELEGRAM_ADMIN_CHAT_ID`
    is unset, so tests and local dev work without touching the network.
    """

    def __init__(self, config: AdminNotifierConfig, *, timeout: float = 5.0) -> None:
        self.config = config
        self.timeout = timeout

    def send(self, html_body: str) -> bool:
        if not self.config.enabled:
            log.debug("admin notifier disabled — skipping send")
            return False
        body = _truncate(html_body, TELEGRAM_MAX_BODY)
        url = TELEGRAM_API_URL.format(token=self.config.token)
        ok = True
        for chat_id in self.config.chat_ids:
            try:
                r = requests.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": body,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=self.timeout,
                )
                if r.status_code != 200:
                    log.warning(
                        "admin notify to %s failed: HTTP %s — %s",
                        chat_id,
                        r.status_code,
                        r.text[:200],
                    )
                    ok = False
                else:
                    log.info("admin notify sent to %s", chat_id)
            except requests.RequestException as e:
                log.warning("admin notify to %s raised: %s", chat_id, e)
                ok = False
        return ok


# ─── message formatters ───────────────────────────────────────────────────


def format_event(
    *,
    headline: str,
    source: str,
    customer: str,
    quarter: str | None = None,
    extras: dict[str, str] | None = None,
    review_note_path: Path | None = None,
) -> str:
    """Build an HTML-mode Telegram message for one IFTA-service event.

    `review_note_path`, when given and present, contributes the `## Agent run
    details` block and the deterministic IFTA Review summary lines that the
    pipeline writes to review_note.md — i.e. the same payload as the example
    in the project request.
    """
    lines: list[str] = [f"<b>{html.escape(headline)}</b>"]
    meta_lines = [f"<b>Source:</b> {html.escape(source)}", f"<b>Customer:</b> {html.escape(customer)}"]
    if quarter:
        meta_lines.append(f"<b>Quarter:</b> {html.escape(quarter)}")
    if extras:
        for k, v in extras.items():
            meta_lines.append(f"<b>{html.escape(k)}:</b> {html.escape(str(v))}")
    lines.append("\n".join(meta_lines))

    if review_note_path is not None:
        excerpt = _extract_review_excerpt(review_note_path)
        if excerpt:
            lines.append(f"<pre>{html.escape(excerpt)}</pre>")
    return "\n\n".join(lines)


def _extract_review_excerpt(path: Path) -> str:
    """Pull the metrics + summary sections out of review_note.md as plain text.

    review_note.md is shaped as:
        # IFTA Review Note
        ## Summary
        <text>
        ## Issues
        - ...
        ## Filing reminders
        - ...
        ## Next steps
        - ...
        ## Agent run details
        - ...

    We keep the Summary + Issues sections (the human-actionable bits) and the
    Agent run details block (the cost/latency receipt). Filing-reminders /
    next-steps drop out so the Telegram message stays under the 4 KB cap on
    big runs — they're already in the emailed packet.
    """
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""

    sections = _split_markdown_sections(text)
    keep_order = ["Summary", "Issues", "Agent run details"]
    parts: list[str] = []
    for key in keep_order:
        body = sections.get(key)
        if body:
            parts.append(f"## {key}\n{body.rstrip()}")
    if not parts:
        # Fallback: just trim the file when we can't recognise its shape.
        return text.strip()
    return "\n\n".join(parts)


def _split_markdown_sections(text: str) -> dict[str, str]:
    """Return {heading: body} for every `## Heading` block. Last-wins on dups."""
    sections: dict[str, str] = {}
    current_key: str | None = None
    current_body: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if current_key is not None:
                sections[current_key] = "\n".join(current_body).strip()
            current_key = m.group(1).strip()
            current_body = []
        else:
            if current_key is not None:
                current_body.append(line)
    if current_key is not None:
        sections[current_key] = "\n".join(current_body).strip()
    return sections


def _truncate(body: str, max_chars: int) -> str:
    if len(body) <= max_chars:
        return body
    return body[: max_chars - 20].rstrip() + "\n…(truncated)"
