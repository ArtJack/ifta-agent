"""Tests for the Resend email wrapper.

We never hit the real API — we monkeypatch `_send_via_resend` and inspect
the params dict the EmailClient builds. That covers attachment shaping,
subject lines, body content, and the no-key short-circuit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ifta.web import email as email_module
from ifta.web.email import EmailClient, EmailConfig, load_email_config_from_env
from ifta.web.models import Submission, SubmissionStatus


@pytest.fixture
def captured_sends(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    def fake_send(params: dict[str, Any]) -> str:
        sent.append(params)
        return "fake-email-id"

    monkeypatch.setattr(email_module, "_send_via_resend", fake_send)
    return sent


def _make_sub(quarter: str = "Q1-2026", email: str = "customer@example.com") -> Submission:
    return Submission(
        id="sid-abc",
        email=email,
        quarter=quarter,
        status=SubmissionStatus.PENDING_CONFIRMATION,
        confirm_token="tok-xyz",
        created_at=datetime.now(UTC),
        company="Test LLC",
    )


def _enabled_config() -> EmailConfig:
    return EmailConfig(
        api_key="re_test",
        from_email="IFTA <ifta@artjeck.com>",
        public_base_url="https://ifta-api.artjeck.com",
        admin_bcc=(),
    )


def test_config_enabled_flag() -> None:
    assert EmailConfig(api_key=None).enabled is False
    assert EmailConfig(api_key="x").enabled is True


def test_load_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEND_API_KEY", "re_xxx")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "From <a@b.co>")
    monkeypatch.setenv("IFTA_WEB_PUBLIC_BASE_URL", "https://example.com/")
    monkeypatch.setenv("IFTA_WEB_ADMIN_BCC", "eugene@artjeck.com, ops@artjeck.com")
    cfg = load_email_config_from_env()
    assert cfg.api_key == "re_xxx"
    assert cfg.from_email == "From <a@b.co>"
    assert cfg.public_base_url == "https://example.com"  # trailing slash trimmed
    assert cfg.admin_bcc == ("eugene@artjeck.com", "ops@artjeck.com")


def test_send_confirmation_disabled_noop() -> None:
    client = EmailClient(EmailConfig(api_key=None))
    assert client.send_confirmation(_make_sub()) is False


def test_send_confirmation_includes_link(captured_sends: list[dict[str, Any]]) -> None:
    client = EmailClient(_enabled_config())
    sub = _make_sub()
    assert client.send_confirmation(sub) is True
    assert len(captured_sends) == 1
    params = captured_sends[0]
    assert params["to"] == [sub.email]
    assert "Q1-2026" in params["subject"]
    assert "https://ifta-api.artjeck.com/confirm/tok-xyz" in params["text"]
    assert "attachments" not in params


def test_send_packet_attaches_all_files(
    captured_sends: list[dict[str, Any]], tmp_path: Path
) -> None:
    out_dir = tmp_path / "outputs" / "Q1-2026"
    out_dir.mkdir(parents=True)
    (out_dir / "ifta_portal.csv").write_bytes(b"state,miles\nKY,100\n")
    (out_dir / "review_note.md").write_text(
        "# IFTA Review — 1Q2026\n\n- Total tax due: **$123.45**\n", encoding="utf-8"
    )
    trucks = out_dir / "trucks"
    trucks.mkdir()
    (trucks / "truck_800.xlsx").write_bytes(b"\x50\x4b\x03\x04fake-xlsx")

    client = EmailClient(_enabled_config())
    assert client.send_packet(_make_sub(), out_dir) is True

    params = captured_sends[0]
    assert "Q1-2026" in params["subject"]
    filenames = {a["filename"] for a in params["attachments"]}
    assert filenames == {"ifta_portal.csv", "review_note.md", "truck_800.xlsx"}
    # Each attachment carries the actual bytes
    portal_att = next(a for a in params["attachments"] if a["filename"] == "ifta_portal.csv")
    assert bytes(portal_att["content"]) == b"state,miles\nKY,100\n"
    # Review note text is embedded in the body for inbox visibility
    assert "Total tax due: **$123.45**" in params["text"]


def test_send_packet_handles_missing_out_dir(captured_sends: list[dict[str, Any]]) -> None:
    client = EmailClient(_enabled_config())
    # Out dir doesn't exist — should still send (just no attachments).
    assert client.send_packet(_make_sub(), Path("/no/such/dir")) is True
    params = captured_sends[0]
    assert params.get("attachments") in (None, [])


def test_send_failure(captured_sends: list[dict[str, Any]]) -> None:
    client = EmailClient(_enabled_config())
    assert client.send_failure(_make_sub(), "ingest blew up on row 12") is True
    params = captured_sends[0]
    assert "Couldn't process" in params["subject"]
    assert "ingest blew up on row 12" in params["text"]


def test_admin_bcc_included(captured_sends: list[dict[str, Any]]) -> None:
    cfg = EmailConfig(
        api_key="re_test",
        admin_bcc=("eugene@artjeck.com",),
    )
    client = EmailClient(cfg)
    client.send_confirmation(_make_sub())
    assert captured_sends[0]["bcc"] == ["eugene@artjeck.com"]


def test_send_failure_swallows_resend_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_params: dict[str, Any]) -> str:
        raise RuntimeError("API down")

    monkeypatch.setattr(email_module, "_send_via_resend", boom)
    client = EmailClient(_enabled_config())
    # Should return False, not propagate — worker callback failures must not
    # crash the polling loop.
    assert client.send_confirmation(_make_sub()) is False
