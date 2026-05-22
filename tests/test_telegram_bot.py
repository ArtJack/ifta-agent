"""Tests for the operator approval bot.

The bot is admin-only and button-driven: it turns the inline approve/reject
callbacks (sent by the web app via ifta.notify) into DB state transitions.
Handlers are async; we drive them with asyncio.run() since the project doesn't
depend on pytest-asyncio.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ifta import telegram_bot as bot
from ifta.web import db
from ifta.web.models import SubmissionStatus

# ─── parse_user_ids ───────────────────────────────────────────────────────────


def test_parse_user_ids_accepts_comma_and_space_separated() -> None:
    assert bot.parse_user_ids("111, 222 333") == (111, 222, 333)
    assert bot.parse_user_ids("") == ()
    assert bot.parse_user_ids(None) == ()


def test_parse_user_ids_rejects_non_numeric() -> None:
    with pytest.raises(ValueError):
        bot.parse_user_ids("111, abc")


# ─── load_bot_config ──────────────────────────────────────────────────────────


def test_load_bot_config_requires_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        bot.load_bot_config(tmp_path)


def test_load_bot_config_reads_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:def")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "111, 222")
    monkeypatch.setenv("IFTA_WEB_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("IFTA_WEB_SUBMISSIONS_DIR", str(tmp_path / "subs"))
    cfg = bot.load_bot_config(tmp_path)
    assert cfg.token == "abc:def"
    assert cfg.admin_user_ids == (111, 222)
    assert cfg.db_path == tmp_path / "jobs.db"
    assert cfg.submissions_dir == tmp_path / "subs"


def test_approval_markup_matches_notify_scheme() -> None:
    markup = bot.approval_markup("sub-9")
    buttons = markup.inline_keyboard[0]
    assert buttons[0].callback_data == "approve:sub-9"
    assert buttons[1].callback_data == "reject:sub-9"


# ─── lightweight Telegram stubs ───────────────────────────────────────────────


@dataclass
class FakeUser:
    id: int


@dataclass
class FakeMessage:
    replies: list[dict] = field(default_factory=list)

    async def reply_text(self, text: str, reply_markup: object | None = None) -> None:
        self.replies.append({"text": text, "reply_markup": reply_markup})


@dataclass
class FakeQuery:
    data: str
    edits: list[str] = field(default_factory=list)
    answers: list[str | None] = field(default_factory=list)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append(text)

    async def edit_message_text(self, text: str) -> None:
        self.edits.append(text)


@dataclass
class FakeUpdate:
    effective_user: FakeUser | None = None
    callback_query: FakeQuery | None = None
    effective_message: FakeMessage | None = None


class FakeContext:
    def __init__(self, config: bot.BotConfig) -> None:
        self.bot_data = {"config": config}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "jobs.db"
    db.init_db(path)
    return path


@pytest.fixture
def config(tmp_path: Path, db_path: Path) -> bot.BotConfig:
    return bot.BotConfig(
        token="t",
        admin_user_ids=(42,),
        db_path=db_path,
        submissions_dir=tmp_path / "subs",
    )


def _make_pending(db_path: Path, sid: str = "s1") -> None:
    db.create_submission(
        db_path,
        submission_id=sid,
        email="ops@blabla.co",
        quarter="Q1-2026",
        confirm_token=f"tok-{sid}",
        company="BLA BLA Transportation",
        trucks=15,
    )


# ─── approval_callback ────────────────────────────────────────────────────────


def test_approve_callback_queues_submission(config: bot.BotConfig, db_path: Path) -> None:
    _make_pending(db_path)
    query = FakeQuery(data="approve:s1")
    update = FakeUpdate(effective_user=FakeUser(42), callback_query=query)
    asyncio.run(bot.approval_callback(update, FakeContext(config)))

    sub = db.get_submission(db_path, "s1")
    assert sub is not None and sub.status == SubmissionStatus.QUEUED
    assert query.edits and "Approved" in query.edits[0]


def test_reject_callback_rejects_submission(config: bot.BotConfig, db_path: Path) -> None:
    _make_pending(db_path)
    query = FakeQuery(data="reject:s1")
    update = FakeUpdate(effective_user=FakeUser(42), callback_query=query)
    asyncio.run(bot.approval_callback(update, FakeContext(config)))

    sub = db.get_submission(db_path, "s1")
    assert sub is not None and sub.status == SubmissionStatus.REJECTED
    assert query.edits and "Rejected" in query.edits[0]


def test_non_admin_callback_does_not_change_state(config: bot.BotConfig, db_path: Path) -> None:
    _make_pending(db_path)
    query = FakeQuery(data="approve:s1")
    update = FakeUpdate(effective_user=FakeUser(999), callback_query=query)  # not admin
    asyncio.run(bot.approval_callback(update, FakeContext(config)))

    sub = db.get_submission(db_path, "s1")
    assert sub is not None and sub.status == SubmissionStatus.PENDING_APPROVAL
    assert not query.edits  # nothing edited
    assert "Not authorized." in query.answers


def test_callback_on_unknown_submission_reports_missing(
    config: bot.BotConfig, db_path: Path
) -> None:
    query = FakeQuery(data="approve:ghost")
    update = FakeUpdate(effective_user=FakeUser(42), callback_query=query)
    asyncio.run(bot.approval_callback(update, FakeContext(config)))
    assert query.edits and "not found" in query.edits[0].lower()


def test_double_approve_is_reported_as_no_change(config: bot.BotConfig, db_path: Path) -> None:
    _make_pending(db_path)
    update1 = FakeUpdate(effective_user=FakeUser(42), callback_query=FakeQuery(data="approve:s1"))
    asyncio.run(bot.approval_callback(update1, FakeContext(config)))
    # A second tap (e.g. another admin) finds it already queued.
    query2 = FakeQuery(data="reject:s1")
    update2 = FakeUpdate(effective_user=FakeUser(42), callback_query=query2)
    asyncio.run(bot.approval_callback(update2, FakeContext(config)))

    sub = db.get_submission(db_path, "s1")
    assert sub is not None and sub.status == SubmissionStatus.QUEUED  # unchanged
    assert query2.edits and "already" in query2.edits[0].lower()


# ─── /pending ─────────────────────────────────────────────────────────────────


def test_pending_lists_awaiting_submissions(config: bot.BotConfig, db_path: Path) -> None:
    _make_pending(db_path, "s1")
    _make_pending(db_path, "s2")
    message = FakeMessage()
    update = FakeUpdate(effective_user=FakeUser(42), effective_message=message)
    asyncio.run(bot.pending_command(update, FakeContext(config)))
    assert len(message.replies) == 2
    assert all("BLA BLA Transportation" in r["text"] for r in message.replies)
    assert all(r["reply_markup"] is not None for r in message.replies)


def test_pending_empty(config: bot.BotConfig, db_path: Path) -> None:
    message = FakeMessage()
    update = FakeUpdate(effective_user=FakeUser(42), effective_message=message)
    asyncio.run(bot.pending_command(update, FakeContext(config)))
    assert len(message.replies) == 1
    assert "No submissions awaiting approval." in message.replies[0]["text"]


def test_pending_rejects_non_admin(config: bot.BotConfig, db_path: Path) -> None:
    _make_pending(db_path)
    message = FakeMessage()
    update = FakeUpdate(effective_user=FakeUser(999), effective_message=message)
    asyncio.run(bot.pending_command(update, FakeContext(config)))
    assert message.replies and "Not authorized." in message.replies[0]["text"]
