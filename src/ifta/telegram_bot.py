"""Operator approval bot for IFTA web submissions.

A small, admin-only Telegram bot. Customers submit their files on the website
(`ifta web`); the web app then pushes an approval request via `ifta.notify`
with inline [✅ Process] / [❌ Reject] buttons. This bot handles those button
taps:

- **Process** → flips the submission PENDING_APPROVAL → QUEUED, so the web
  worker runs the (paid) AI pipeline and emails the customer their packet.
- **Reject** → flips it PENDING_APPROVAL → REJECTED; the pipeline never runs.

It also exposes `/pending` to re-list submissions awaiting a decision and
`/start` for a sanity check. This bot never receives customer files — all
intake happens on the website. Only Telegram user IDs in
`TELEGRAM_ADMIN_USER_IDS` may use it.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from ifta.notify import APPROVE_PREFIX, REJECT_PREFIX, parse_callback_data
from ifta.web import db
from ifta.web.models import Submission, SubmissionStatus

log = logging.getLogger("ifta.telegram_bot")

# Repo root: src/ifta/telegram_bot.py → ifta → src → <root>.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Mirrors ifta.web.app.get_db_path / get_submissions_dir so the bot reads the
# same SQLite job state the web app writes. Kept in sync via the env-var names.
_DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "web_jobs.db"
_DEFAULT_SUBMISSIONS_DIR = PROJECT_ROOT / "data" / "web_submissions"


@dataclass
class BotConfig:
    token: str
    admin_user_ids: tuple[int, ...]
    db_path: Path
    submissions_dir: Path
    project_root: Path = PROJECT_ROOT


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
            "put TELEGRAM_BOT_TOKEN=<token> in .env."
        )
    db_env = os.environ.get("IFTA_WEB_DB_PATH")
    subs_env = os.environ.get("IFTA_WEB_SUBMISSIONS_DIR")
    return BotConfig(
        token=token,
        admin_user_ids=parse_user_ids(os.environ.get("TELEGRAM_ADMIN_USER_IDS")),
        db_path=Path(db_env) if db_env else _DEFAULT_DB_PATH,
        submissions_dir=Path(subs_env) if subs_env else _DEFAULT_SUBMISSIONS_DIR,
        project_root=project_root,
    )


# ─── helpers ────────────────────────────────────────────────────────────────


def _config(context: ContextTypes.DEFAULT_TYPE) -> BotConfig:
    return context.bot_data["config"]


def _is_admin(update: Update, config: BotConfig) -> bool:
    user = update.effective_user
    return bool(user and user.id in config.admin_user_ids)


def approval_markup(submission_id: str) -> InlineKeyboardMarkup:
    """Inline keyboard matching ifta.notify's approve/reject callback scheme."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Process", callback_data=f"{APPROVE_PREFIX}:{submission_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"{REJECT_PREFIX}:{submission_id}"),
            ]
        ]
    )


def _summary_line(sub: Submission) -> str:
    trucks = sub.trucks if sub.trucks is not None else "?"
    company = sub.company or "Unknown company"
    return f"🚚 {company} — {sub.quarter}\nTrucks: {trucks}\nContact: {sub.email}"


# ─── handlers ───────────────────────────────────────────────────────────────


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    message = update.effective_message
    if message is None:
        return
    if _is_admin(update, config):
        await message.reply_text(
            "IFTA approval bot is running.\n"
            "You'll get an approval request here whenever a customer submits. "
            "Use /pending to re-list submissions awaiting a decision."
        )
        return
    uid = update.effective_user.id if update.effective_user else "unknown"
    await message.reply_text(
        "This is a private operator bot.\n"
        f"Your Telegram ID is {uid}. Ask the admin to add it to "
        "TELEGRAM_ADMIN_USER_IDS to use it."
    )


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    message = update.effective_message
    if message is None:
        return
    if not _is_admin(update, config):
        await message.reply_text("Not authorized.")
        return
    subs = db.list_submissions(config.db_path, status=SubmissionStatus.PENDING_APPROVAL)
    if not subs:
        await message.reply_text("No submissions awaiting approval.")
        return
    for sub in subs:
        await message.reply_text(_summary_line(sub), reply_markup=approval_markup(sub.id))


async def approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not _is_admin(update, config):
        await query.answer("Not authorized.", show_alert=True)
        return
    parsed = parse_callback_data(query.data or "")
    if parsed is None:
        return
    action, submission_id = parsed

    if action == APPROVE_PREFIX:
        sub = db.approve_submission(config.db_path, submission_id)
        verb, emoji = "approved", "✅"
    else:
        sub = db.reject_submission(config.db_path, submission_id)
        verb, emoji = "rejected", "❌"

    if sub is None:
        await query.edit_message_text("⚠️ Submission not found (it may have been removed).")
        return

    company = sub.company or "Unknown company"
    if action == APPROVE_PREFIX and sub.status == SubmissionStatus.QUEUED:
        text = f"{emoji} Approved — {company} {sub.quarter} is queued for processing."
    elif action == REJECT_PREFIX and sub.status == SubmissionStatus.REJECTED:
        text = f"{emoji} Rejected — {company} {sub.quarter} will not be processed."
    else:
        # The row had already moved on (decided earlier, or already running/done).
        text = (
            f"ℹ️ {company} {sub.quarter} is already “{sub.status.value}”. "
            f"No change made (could not be {verb})."
        )
    await query.edit_message_text(text)


# ─── application wiring ───────────────────────────────────────────────────────


def build_application(config: BotConfig) -> Application:
    app = ApplicationBuilder().token(config.token).build()
    app.bot_data["config"] = config
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(CommandHandler("pending", pending_command))
    app.add_handler(
        CallbackQueryHandler(
            approval_callback,
            pattern=rf"^(?:{APPROVE_PREFIX}|{REJECT_PREFIX}):",
        )
    )
    return app


def run_polling(config: BotConfig) -> None:
    app = build_application(config)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
