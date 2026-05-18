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
    assert files == {"miles.csv", "fuel.csv"}
    assert (inbox / "miles.csv").read_bytes() == miles_csv
    assert (inbox / "fuel.csv").read_bytes() == fuel_csv


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
    # Force the verifier to reject.
    from ifta.web import app as app_module

    monkeypatch.setattr(app_module, "verify_turnstile_token", lambda *a, **kw: False)
    test_client = TestClient(app_module.create_app())

    r = test_client.post(
        "/submit",
        data={
            "email": "a@b.co",
            "quarter": "Q1-2026",
            "cf_turnstile_response": "bogus-token",
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
    from ifta.web import app as app_module

    monkeypatch.setattr(app_module, "verify_turnstile_token", lambda *a, **kw: True)
    test_client = TestClient(app_module.create_app())

    r = test_client.post(
        "/submit",
        data={
            "email": "a@b.co",
            "quarter": "Q1-2026",
            "cf_turnstile_response": "good-token",
        },
        files={
            "mileage_file": ("m.csv", b"x", "text/csv"),
            "fuel_file": ("f.csv", b"y", "text/csv"),
        },
    )
    assert r.status_code == 202


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
