"""Tests for the admin Telegram notifier."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ifta import notify


def test_disabled_when_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "12345")
    cfg = notify.load_admin_notifier_config()
    assert cfg.enabled is False
    n = notify.AdminNotifier(cfg)
    assert n.send("anything") is False


def test_disabled_when_no_chat_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:def")
    monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_USER_IDS", raising=False)
    cfg = notify.load_admin_notifier_config()
    assert cfg.enabled is False


def test_falls_back_to_admin_user_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:def")
    monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "111, 222")
    cfg = notify.load_admin_notifier_config()
    assert cfg.enabled is True
    assert cfg.chat_ids == (111, 222)


def test_send_posts_to_each_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "BOT-TOKEN")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "111,222")
    cfg = notify.load_admin_notifier_config()

    posts: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200
        text = "ok"

    def fake_post(url: str, json: dict[str, Any], timeout: float) -> FakeResponse:
        posts.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(notify.requests, "post", fake_post)

    n = notify.AdminNotifier(cfg)
    assert n.send("<b>hello</b>") is True
    assert len(posts) == 2
    assert all(p["url"].endswith("/botBOT-TOKEN/sendMessage") for p in posts)
    chat_ids = sorted(p["json"]["chat_id"] for p in posts)
    assert chat_ids == [111, 222]
    assert posts[0]["json"]["parse_mode"] == "HTML"
    assert posts[0]["json"]["text"] == "<b>hello</b>"


def test_send_returns_false_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "BOT-TOKEN")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "111")
    cfg = notify.load_admin_notifier_config()

    class FakeResponse:
        status_code = 500
        text = "internal error"

    monkeypatch.setattr(
        notify.requests, "post", lambda *a, **kw: FakeResponse()
    )
    n = notify.AdminNotifier(cfg)
    assert n.send("body") is False


def test_send_swallows_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "BOT-TOKEN")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "111")
    cfg = notify.load_admin_notifier_config()

    def raise_(*_a: Any, **_kw: Any) -> None:
        raise notify.requests.ConnectionError("boom")

    monkeypatch.setattr(notify.requests, "post", raise_)
    n = notify.AdminNotifier(cfg)
    assert n.send("body") is False  # logged and returned, no raise


def test_format_event_basic_html() -> None:
    msg = notify.format_event(
        headline="✅ IFTA packet delivered",
        source="web intake",
        customer="x@y.com",
        quarter="Q1-2026",
        extras={"Company": "Acme & Co", "Submission": "abc123"},
    )
    assert "<b>✅ IFTA packet delivered</b>" in msg
    assert "<b>Source:</b> web intake" in msg
    assert "<b>Customer:</b> x@y.com" in msg
    assert "<b>Quarter:</b> Q1-2026" in msg
    # HTML-escapes ampersands so Telegram parse_mode=HTML doesn't choke.
    assert "Acme &amp; Co" in msg


def test_format_event_embeds_review_excerpt(tmp_path: Path) -> None:
    review = tmp_path / "review_note.md"
    review.write_text(
        "# IFTA Review Note\n\n"
        "## Summary\n"
        "Fleet looks fine.\n\n"
        "## Issues\n"
        "- nothing material\n\n"
        "## Filing reminders\n"
        "- pay before Apr 30\n\n"
        "## Agent run details\n\n"
        "- **Model:** `claude-opus-4-7`\n"
        "- **Wall time:** 35.6s\n",
        encoding="utf-8",
    )
    msg = notify.format_event(
        headline="✅ Done",
        source="web intake",
        customer="x@y.com",
        review_note_path=review,
    )
    assert "<pre>" in msg and "</pre>" in msg
    assert "## Summary" in msg
    assert "Fleet looks fine." in msg
    assert "## Issues" in msg
    assert "## Agent run details" in msg
    # Dropped to stay short — filing-reminders/next-steps belong only in email.
    assert "Filing reminders" not in msg
    assert "pay before Apr 30" not in msg


def test_format_event_skips_excerpt_when_file_missing(tmp_path: Path) -> None:
    msg = notify.format_event(
        headline="✅ Done",
        source="web intake",
        customer="x@y.com",
        review_note_path=tmp_path / "nope.md",
    )
    assert "<pre>" not in msg


def test_truncate_keeps_body_under_cap() -> None:
    huge = "x" * 10_000
    out = notify._truncate(huge, 100)
    assert len(out) <= 100
    assert out.endswith("…(truncated)")
