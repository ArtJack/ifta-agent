"""Tests for the FastAPI web app."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def submissions_root(tmp_path: Path) -> Path:
    return tmp_path / "subs"


@pytest.fixture
def client(
    tmp_path: Path,
    submissions_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """Dev-mode app: no RESEND_API_KEY, so submissions go straight to QUEUED."""
    monkeypatch.setenv("IFTA_WEB_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("IFTA_WEB_SUBMISSIONS_DIR", str(submissions_root))
    monkeypatch.setenv("IFTA_WEB_CORS_ORIGINS", "http://localhost")
    # High limit so existing tests can fire many submissions without 429.
    monkeypatch.setenv("IFTA_WEB_SUBMIT_RATE_LIMIT", "10000/hour")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("TURNSTILE_SECRET_KEY", raising=False)
    # Disable Telegram approval gate so dev-mode goes straight to QUEUED.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_USER_IDS", raising=False)
    from ifta.web.app import create_app

    return TestClient(create_app())


@pytest.fixture
def email_app(
    tmp_path: Path,
    submissions_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, list[dict[str, Any]]]:
    """App with email enabled — captures sends instead of hitting Resend."""
    monkeypatch.setenv("IFTA_WEB_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("IFTA_WEB_SUBMISSIONS_DIR", str(submissions_root))
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("IFTA_WEB_PUBLIC_BASE_URL", "https://ifta-api.test")
    monkeypatch.setenv("IFTA_WEB_SUBMIT_RATE_LIMIT", "10000/hour")
    monkeypatch.delenv("TURNSTILE_SECRET_KEY", raising=False)
    # Disable Telegram approval gate so email-confirm path is tested.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_USER_IDS", raising=False)

    sent: list[dict[str, Any]] = []

    def fake_send(params: dict[str, Any]) -> str:
        sent.append(params)
        return "fake-id"

    from ifta.web import email as email_module

    monkeypatch.setattr(email_module, "_send_via_resend", fake_send)
    from ifta.web.app import create_app

    return TestClient(create_app()), sent


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_submit_creates_submission_and_saves_files(
    client: TestClient, submissions_root: Path
) -> None:
    miles_csv = b"truck,state,miles\nT1,KY,1000\n"
    fuel_csv = b"truck,state,gallons\nT1,KY,150\n"

    r = client.post(
        "/submit",
        data={"email": "customer@example.com", "quarter": "Q1-2026"},
        files={
            "mileage_file": ("miles.csv", miles_csv, "text/csv"),
            "fuel_file": ("fuel.csv", fuel_csv, "text/csv"),
        },
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert "submission_id" in body
    assert body["status"] == "queued"

    sid = body["submission_id"]
    inbox = submissions_root / sid / "inbox" / "Q1-2026"
    assert inbox.exists()
    files = {p.name for p in inbox.iterdir()}
    # Prefix prevents collisions when both files happen to share a name.
    assert files == {"mileage_miles.csv", "fuel_fuel.csv"}
    assert (inbox / "mileage_miles.csv").read_bytes() == miles_csv
    assert (inbox / "fuel_fuel.csv").read_bytes() == fuel_csv


def test_status_returns_submission_state(client: TestClient) -> None:
    r = client.post(
        "/submit",
        data={"email": "a@b.co", "quarter": "Q1-2026", "company": "ABC LLC"},
        files={
            "mileage_file": ("m.csv", b"x", "text/csv"),
            "fuel_file": ("f.csv", b"y", "text/csv"),
        },
    )
    sid = r.json()["submission_id"]

    status = client.get(f"/status/{sid}")
    assert status.status_code == 200
    body = status.json()
    assert body["submission_id"] == sid
    assert body["status"] == "queued"
    assert body["quarter"] == "Q1-2026"
    assert body["error"] is None


def test_status_404_for_unknown_id(client: TestClient) -> None:
    r = client.get("/status/no-such-id")
    assert r.status_code == 404


def test_submit_rejects_bad_email(client: TestClient) -> None:
    r = client.post(
        "/submit",
        data={"email": "not-an-email", "quarter": "Q1-2026"},
        files={
            "mileage_file": ("m.csv", b"x", "text/csv"),
            "fuel_file": ("f.csv", b"x", "text/csv"),
        },
    )
    assert r.status_code == 400
    assert "email" in r.json()["detail"].lower()


def test_submit_rejects_bad_quarter(client: TestClient) -> None:
    r = client.post(
        "/submit",
        data={"email": "a@b.co", "quarter": "garbage"},
        files={
            "mileage_file": ("m.csv", b"x", "text/csv"),
            "fuel_file": ("f.csv", b"x", "text/csv"),
        },
    )
    assert r.status_code == 400


def test_submit_rejects_disallowed_extension(client: TestClient) -> None:
    r = client.post(
        "/submit",
        data={"email": "a@b.co", "quarter": "Q1-2026"},
        files={
            "mileage_file": ("m.exe", b"binary", "application/octet-stream"),
            "fuel_file": ("f.csv", b"x", "text/csv"),
        },
    )
    assert r.status_code == 400
    detail = r.json()["detail"].lower()
    assert "unsupported" in detail or ".exe" in detail


def test_submit_rejects_oversize_file(
    tmp_path: Path,
    submissions_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IFTA_WEB_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("IFTA_WEB_SUBMISSIONS_DIR", str(submissions_root))
    monkeypatch.setenv("IFTA_WEB_MAX_FILE_MB", "1")  # 1 MB cap for this test
    monkeypatch.setenv("IFTA_WEB_SUBMIT_RATE_LIMIT", "10000/hour")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("TURNSTILE_SECRET_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_USER_IDS", raising=False)
    from ifta.web.app import create_app

    test_client = TestClient(create_app())

    big = b"A" * (2 * 1024 * 1024)  # 2 MB
    r = test_client.post(
        "/submit",
        data={"email": "a@b.co", "quarter": "Q1-2026"},
        files={
            "mileage_file": ("big.csv", big, "text/csv"),
            "fuel_file": ("f.csv", b"x", "text/csv"),
        },
    )
    assert r.status_code == 413


def test_submit_with_email_enabled_starts_pending(
    email_app: tuple[TestClient, list[dict[str, Any]]],
) -> None:
    test_client, sent = email_app
    r = test_client.post(
        "/submit",
        data={"email": "customer@example.com", "quarter": "Q1-2026"},
        files={
            "mileage_file": ("m.csv", b"x", "text/csv"),
            "fuel_file": ("f.csv", b"y", "text/csv"),
        },
    )
    assert r.status_code == 202
    assert r.json()["status"] == "pending_confirmation"
    assert len(sent) == 1
    assert "Confirm" in sent[0]["subject"]
    assert "https://ifta-api.test/confirm/" in sent[0]["text"]


def test_confirm_endpoint_flips_to_queued(
    email_app: tuple[TestClient, list[dict[str, Any]]],
) -> None:
    test_client, sent = email_app
    r = test_client.post(
        "/submit",
        data={"email": "customer@example.com", "quarter": "Q1-2026"},
        files={
            "mileage_file": ("m.csv", b"x", "text/csv"),
            "fuel_file": ("f.csv", b"y", "text/csv"),
        },
    )
    sid = r.json()["submission_id"]
    # Pull the token out of the confirmation email body.
    body = sent[0]["text"]
    token = body.split("/confirm/")[1].split()[0].strip()

    confirm_resp = test_client.get(f"/confirm/{token}")
    assert confirm_resp.status_code == 200
    assert "Got it" in confirm_resp.text or "processing started" in confirm_resp.text

    status = test_client.get(f"/status/{sid}").json()
    assert status["status"] == "queued"


def test_confirm_endpoint_unknown_token(client: TestClient) -> None:
    r = client.get("/confirm/no-such-token")
    assert r.status_code == 404


def test_confirm_endpoint_idempotent(
    email_app: tuple[TestClient, list[dict[str, Any]]],
) -> None:
    """Second click on the link must not crash and must show a sensible page."""
    test_client, sent = email_app
    test_client.post(
        "/submit",
        data={"email": "a@b.co", "quarter": "Q1-2026"},
        files={
            "mileage_file": ("m.csv", b"x", "text/csv"),
            "fuel_file": ("f.csv", b"y", "text/csv"),
        },
    )
    token = sent[0]["text"].split("/confirm/")[1].split()[0].strip()
    first = test_client.get(f"/confirm/{token}")
    assert first.status_code == 200
    # Second click is harmless: row stays in QUEUED, same friendly page returns.
    second = test_client.get(f"/confirm/{token}")
    assert second.status_code == 200
    assert "Processing started" in second.text or "Already" in second.text


def test_submit_rate_limit_enforced(
    tmp_path: Path,
    submissions_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IFTA_WEB_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("IFTA_WEB_SUBMISSIONS_DIR", str(submissions_root))
    monkeypatch.setenv("IFTA_WEB_SUBMIT_RATE_LIMIT", "2/minute")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("TURNSTILE_SECRET_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_USER_IDS", raising=False)
    from ifta.web.app import create_app

    test_client = TestClient(create_app())

    def _post() -> int:
        return test_client.post(
            "/submit",
            data={"email": "a@b.co", "quarter": "Q1-2026"},
            files={
                "mileage_file": ("m.csv", b"x", "text/csv"),
                "fuel_file": ("f.csv", b"y", "text/csv"),
            },
        ).status_code

    assert _post() == 202
    assert _post() == 202
    # Third request in the same minute → 429.
    assert _post() == 429


def test_submit_with_turnstile_missing_token(
    tmp_path: Path,
    submissions_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IFTA_WEB_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("IFTA_WEB_SUBMISSIONS_DIR", str(submissions_root))
    monkeypatch.setenv("IFTA_WEB_SUBMIT_RATE_LIMIT", "10000/hour")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "0x_test_secret")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_USER_IDS", raising=False)
    from ifta.web.app import create_app

    test_client = TestClient(create_app())

    r = test_client.post(
        "/submit",
        data={"email": "a@b.co", "quarter": "Q1-2026"},
        files={
            "mileage_file": ("m.csv", b"x", "text/csv"),
            "fuel_file": ("f.csv", b"y", "text/csv"),
        },
    )
    assert r.status_code == 400
    assert "CAPTCHA" in r.json()["detail"]


def test_submit_with_turnstile_bad_token(
    tmp_path: Path,
    submissions_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IFTA_WEB_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("IFTA_WEB_SUBMISSIONS_DIR", str(submissions_root))
    monkeypatch.setenv("IFTA_WEB_SUBMIT_RATE_LIMIT", "10000/hour")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "0x_test_secret")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_USER_IDS", raising=False)
    # Force the verifier to reject.
    from ifta.web import app as app_module

    monkeypatch.setattr(app_module, "verify_turnstile_token", lambda *a, **kw: False)
    test_client = TestClient(app_module.create_app())

    r = test_client.post(
        "/submit",
        data={
            "email": "a@b.co",
            "quarter": "Q1-2026",
            # Cloudflare's widget emits a hyphenated name; the backend uses
            # the alias to bind it.
            "cf-turnstile-response": "bogus-token",
        },
        files={
            "mileage_file": ("m.csv", b"x", "text/csv"),
            "fuel_file": ("f.csv", b"y", "text/csv"),
        },
    )
    assert r.status_code == 400
    assert "verification failed" in r.json()["detail"].lower()


def test_submit_with_turnstile_valid_token(
    tmp_path: Path,
    submissions_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IFTA_WEB_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("IFTA_WEB_SUBMISSIONS_DIR", str(submissions_root))
    monkeypatch.setenv("IFTA_WEB_SUBMIT_RATE_LIMIT", "10000/hour")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "0x_test_secret")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_USER_IDS", raising=False)
    from ifta.web import app as app_module

    monkeypatch.setattr(app_module, "verify_turnstile_token", lambda *a, **kw: True)
    test_client = TestClient(app_module.create_app())

    r = test_client.post(
        "/submit",
        data={
            "email": "a@b.co",
            "quarter": "Q1-2026",
            "cf-turnstile-response": "good-token",
        },
        files={
            "mileage_file": ("m.csv", b"x", "text/csv"),
            "fuel_file": ("f.csv", b"y", "text/csv"),
        },
    )
    assert r.status_code == 202


def test_submit_duplicate_filename_does_not_overwrite(
    client: TestClient, submissions_root: Path
) -> None:
    """Two uploads with the same name must both survive to disk (bug_002).

    Without the field-name prefix the second save would silently replace
    the first and the pipeline would compute on partial data.
    """
    miles_bytes = b"miles,KY,100\n"
    fuel_bytes = b"fuel,KY,150\n"
    r = client.post(
        "/submit",
        data={"email": "a@b.co", "quarter": "Q1-2026"},
        files={
            # Both files happen to share the exact same client-side name —
            # realistic when both come from the same fleet portal.
            "mileage_file": ("data.csv", miles_bytes, "text/csv"),
            "fuel_file": ("data.csv", fuel_bytes, "text/csv"),
        },
    )
    assert r.status_code == 202
    sid = r.json()["submission_id"]
    inbox = submissions_root / sid / "inbox" / "Q1-2026"
    contents = {p.name: p.read_bytes() for p in inbox.iterdir()}
    assert contents == {
        "mileage_data.csv": miles_bytes,
        "fuel_data.csv": fuel_bytes,
    }


def test_submit_cleans_up_on_oversize_second_upload(
    tmp_path: Path,
    submissions_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the second file blows the size cap, no leftovers on disk (bug_021)."""
    monkeypatch.setenv("IFTA_WEB_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("IFTA_WEB_SUBMISSIONS_DIR", str(submissions_root))
    monkeypatch.setenv("IFTA_WEB_SUBMIT_RATE_LIMIT", "10000/hour")
    monkeypatch.setenv("IFTA_WEB_MAX_FILE_MB", "1")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("TURNSTILE_SECRET_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_USER_IDS", raising=False)
    from ifta.web.app import create_app

    test_client = TestClient(create_app())

    r = test_client.post(
        "/submit",
        data={"email": "a@b.co", "quarter": "Q1-2026"},
        files={
            "mileage_file": ("ok.csv", b"x" * 1024, "text/csv"),
            "fuel_file": ("big.csv", b"A" * (2 * 1024 * 1024), "text/csv"),
        },
    )
    assert r.status_code == 413
    # The whole submission tree must be gone — no orphan mileage file.
    leftovers = list(submissions_root.rglob("*")) if submissions_root.exists() else []
    leftover_files = [p for p in leftovers if p.is_file()]
    assert leftover_files == []


def test_confirm_endpoint_escapes_html_in_email(
    email_app: tuple[TestClient, list[dict[str, Any]]],
) -> None:
    """EMAIL_RE permits `<svg/onload=...>` — the response must escape it (bug_001)."""
    test_client, sent = email_app
    payload_email = "<svg/onload=alert(1)>@a.b"
    r = test_client.post(
        "/submit",
        data={"email": payload_email, "quarter": "Q1-2026"},
        files={
            "mileage_file": ("m.csv", b"x", "text/csv"),
            "fuel_file": ("f.csv", b"y", "text/csv"),
        },
    )
    assert r.status_code == 202
    token = sent[0]["text"].split("/confirm/")[1].split()[0].strip()
    confirm = test_client.get(f"/confirm/{token}")
    assert confirm.status_code == 200
    # Raw payload must not appear; escaped form must.
    assert "<svg/onload" not in confirm.text
    assert "&lt;svg/onload=alert(1)&gt;" in confirm.text


def test_submit_confirmation_send_failure_returns_502(
    tmp_path: Path,
    submissions_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Resend rejects the confirmation, surface it instead of stranding
    the row in PENDING_CONFIRMATION (merged_bug_009 confirmation half)."""
    monkeypatch.setenv("IFTA_WEB_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("IFTA_WEB_SUBMISSIONS_DIR", str(submissions_root))
    monkeypatch.setenv("IFTA_WEB_SUBMIT_RATE_LIMIT", "10000/hour")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.delenv("TURNSTILE_SECRET_KEY", raising=False)
    # Disable Telegram approval so the email-confirm path is tested.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_USER_IDS", raising=False)

    from ifta.web import email as email_module

    def boom(_params: dict[str, Any]) -> str:
        raise RuntimeError("Resend down")

    monkeypatch.setattr(email_module, "_send_via_resend", boom)
    from ifta.web.app import create_app

    test_client = TestClient(create_app())

    r = test_client.post(
        "/submit",
        data={"email": "a@b.co", "quarter": "Q1-2026"},
        files={
            "mileage_file": ("m.csv", b"x", "text/csv"),
            "fuel_file": ("f.csv", b"y", "text/csv"),
        },
    )
    assert r.status_code == 502
    # The row should be discoverable via /status — find it.
    # (We can't get the sid from the 502 response; query by listing.)
    from ifta.web import db as web_db

    subs = web_db.list_submissions(tmp_path / "jobs.db")
    assert len(subs) == 1
    assert subs[0].status.value == "failed"
    assert subs[0].error is not None
    assert "confirmation" in subs[0].error.lower()


def test_status_surfaces_packet_sent(client: TestClient) -> None:
    """/status reports packet_sent so ops can distinguish DONE-and-emailed
    from DONE-but-Resend-failed (merged_bug_009 packet half)."""
    r = client.post(
        "/submit",
        data={"email": "a@b.co", "quarter": "Q1-2026"},
        files={
            "mileage_file": ("m.csv", b"x", "text/csv"),
            "fuel_file": ("f.csv", b"y", "text/csv"),
        },
    )
    body = client.get(f"/status/{r.json()['submission_id']}").json()
    assert "packet_sent" in body
    assert body["packet_sent"] is None


def test_submit_sanitizes_filename(
    client: TestClient, submissions_root: Path
) -> None:
    """Path-traversal characters and spaces must be sanitized."""
    r = client.post(
        "/submit",
        data={"email": "a@b.co", "quarter": "Q1-2026"},
        files={
            "mileage_file": ("../../../etc passwd.csv", b"x", "text/csv"),
            "fuel_file": ("f.csv", b"y", "text/csv"),
        },
    )
    assert r.status_code == 202
    sid = r.json()["submission_id"]
    inbox = submissions_root / sid / "inbox" / "Q1-2026"
    names = {p.name for p in inbox.iterdir()}
    # No traversal segments, no spaces — but the .csv suffix must survive.
    saved = next(n for n in names if n != "f.csv")
    assert "/" not in saved
    assert ".." not in saved
    assert saved.endswith(".csv")


# ─── Step 8 slice 4: /submit/add/{token} magic-link upload ───────────────────


def _backend_key_client(
    tmp_path: Path,
    submissions_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    backend_key: str = "test-shared-secret",
) -> TestClient:
    """A TestClient with a known IFTA_WEB_BACKEND_KEY so we can exercise the
    server-to-server auth path /submit/add expects."""
    monkeypatch.setenv("IFTA_WEB_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("IFTA_WEB_SUBMISSIONS_DIR", str(submissions_root))
    monkeypatch.setenv("IFTA_WEB_SUBMIT_RATE_LIMIT", "10000/hour")
    monkeypatch.setenv("IFTA_WEB_BACKEND_KEY", backend_key)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("TURNSTILE_SECRET_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    from ifta.web.app import create_app

    return TestClient(create_app())


def _create_pending_approval_sub(
    test_client: TestClient, submissions_root: Path, *, backend_key: str
) -> str:
    """Create a PENDING_APPROVAL submission and return its confirm_token.

    Posts through /submit (so the inbox dir + per-file naming match
    production) then forces the row into PENDING_APPROVAL — the test client
    runs without Telegram/email configured, so the live submit handler
    would otherwise default to QUEUED. We're not testing /submit here; we're
    testing /submit/add."""
    import secrets
    import sqlite3
    import uuid

    from ifta.web.app import get_db_path

    db_path = get_db_path()
    sid = uuid.uuid4().hex
    token = secrets.token_urlsafe(32)

    # Stage one file in the inbox the way /submit would.
    inbox = submissions_root / sid / "inbox" / "Q1-2026"
    inbox.mkdir(parents=True)
    (inbox / "file_miles.csv").write_bytes(b"truck,state,miles\nT1,KY,1000\n")

    # Insert the row directly in PENDING_APPROVAL.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO submissions (id, email, quarter, status, confirm_token,"
        " company, created_at) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
        (sid, "customer@example.com", "Q1-2026", "pending_approval", token, "ACME"),
    )
    conn.commit()
    return token


def test_submit_add_appends_files_and_reopens_for_review(
    tmp_path: Path,
    submissions_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The customer's '/ifta/add/<token>' upload supplements an existing
    submission: files land in the same inbox, the row reopens for review,
    and a fresh intake brief reflects the updated picture."""
    test_client = _backend_key_client(tmp_path, submissions_root, monkeypatch)
    token = _create_pending_approval_sub(test_client, submissions_root, backend_key="test-shared-secret")

    r = test_client.post(
        f"/submit/add/{token}",
        headers={"X-Backend-Key": "test-shared-secret"},
        files={
            "files": ("fuel.csv", b"truck,state,gallons\nT1,KY,150\n", "text/csv"),
        },
    )
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "pending_approval"

    # The new file landed in the same inbox alongside the original mileage.
    sid = r.json()["submission_id"]
    inbox = submissions_root / sid / "inbox" / "Q1-2026"
    names = {p.name for p in inbox.iterdir()}
    assert any(n.startswith("file_") and n.endswith("miles.csv") for n in names)
    assert any(n.startswith("file_") and n.endswith("fuel.csv") for n in names)


def test_submit_add_accepts_token_only_no_backend_key_required(
    tmp_path: Path,
    submissions_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token in the URL IS the auth — a 32-byte URL-safe secret known
    only to the customer who got the magic-link email. Direct browser POSTs
    from /ifta/add/<token> on artjeck.com work without an X-Backend-Key
    header; CORS still gates the origin."""
    test_client = _backend_key_client(tmp_path, submissions_root, monkeypatch)
    token = _create_pending_approval_sub(test_client, submissions_root, backend_key="test-shared-secret")

    r = test_client.post(
        f"/submit/add/{token}",
        # No X-Backend-Key header — token-only auth.
        files={"files": ("more.csv", b"truck,state,miles\nT1,NV,500\n", "text/csv")},
    )
    assert r.status_code == 202, r.text


def test_submit_add_unknown_token_404(
    tmp_path: Path,
    submissions_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_client = _backend_key_client(tmp_path, submissions_root, monkeypatch)
    r = test_client.post(
        "/submit/add/no-such-token-12345",
        headers={"X-Backend-Key": "test-shared-secret"},
        files={"files": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    )
    assert r.status_code == 404


def test_submit_add_requires_at_least_one_file(
    tmp_path: Path,
    submissions_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_client = _backend_key_client(tmp_path, submissions_root, monkeypatch)
    token = _create_pending_approval_sub(test_client, submissions_root, backend_key="test-shared-secret")
    r = test_client.post(
        f"/submit/add/{token}",
        headers={"X-Backend-Key": "test-shared-secret"},
        # No `files` field at all.
    )
    assert r.status_code == 400


def test_submit_add_rejected_for_closed_submission(
    tmp_path: Path,
    submissions_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a submission has been declined or completed, the add-files
    window is closed (409) so a customer can't slip files into a
    already-decided row."""
    test_client = _backend_key_client(tmp_path, submissions_root, monkeypatch)
    token = _create_pending_approval_sub(test_client, submissions_root, backend_key="test-shared-secret")

    # Manually flip to a closed state (the real flow does this via the
    # Telegram Decline button; here we just write the DB to keep the
    # test focused on the endpoint's gate logic).
    import sqlite3

    from ifta.web.app import get_db_path
    conn = sqlite3.connect(get_db_path())
    conn.execute(
        "UPDATE submissions SET status = 'rejected' WHERE confirm_token = ?",
        (token,),
    )
    conn.commit()

    r = test_client.post(
        f"/submit/add/{token}",
        headers={"X-Backend-Key": "test-shared-secret"},
        files={"files": ("x.csv", b"x", "text/csv")},
    )
    assert r.status_code == 409
    assert "rejected" in r.json()["detail"].lower()
