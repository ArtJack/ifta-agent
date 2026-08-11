"""Web-layer security hardening from the 2026-06-24 audit.

Covers: decompression-bomb guard on zip-based uploads, per-submission file-count
cap, constant-time backend-key comparison, and Turnstile fail-closed.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _make_zip(path: Path, entries: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)


def _dev_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IFTA_WEB_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("IFTA_WEB_SUBMISSIONS_DIR", str(tmp_path / "subs"))
    monkeypatch.setenv("IFTA_WEB_CORS_ORIGINS", "http://localhost")
    monkeypatch.setenv("IFTA_WEB_SUBMIT_RATE_LIMIT", "10000/hour")
    for var in (
        "RESEND_API_KEY",
        "TURNSTILE_SECRET_KEY",
        "IFTA_WEB_BACKEND_KEY",
        "IFTA_WEB_REQUIRE_TURNSTILE",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ADMIN_CHAT_ID",
        "TELEGRAM_ADMIN_USER_IDS",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    _dev_env(tmp_path, monkeypatch)
    from ifta.web.app import create_app

    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Decompression-bomb guard
# ---------------------------------------------------------------------------


def test_archive_guard_rejects_high_ratio_bomb(tmp_path: Path) -> None:
    from ifta.web.app import _archive_is_safe

    bomb = tmp_path / "bomb.xlsx"
    _make_zip(bomb, [("big.bin", b"\0" * 2_000_000)])  # ~2 MB zeros, tiny compressed
    assert _archive_is_safe(bomb) is False


def test_archive_guard_rejects_oversized_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ifta.web.app import _archive_is_safe

    monkeypatch.setenv("IFTA_WEB_MAX_UNCOMPRESSED_MB", "1")
    big = tmp_path / "big.xlsx"
    _make_zip(big, [("a.bin", b"A" * 700_000), ("b.bin", b"B" * 700_000)])
    assert _archive_is_safe(big) is False


def test_archive_guard_allows_normal_xlsx_and_csv(tmp_path: Path) -> None:
    from ifta.web.app import _archive_is_safe

    ok = tmp_path / "ok.xlsx"
    _make_zip(ok, [("xl/worksheets/sheet1.xml", b"<sheet>data</sheet>" * 50)])
    assert _archive_is_safe(ok) is True

    csv = tmp_path / "data.csv"
    csv.write_bytes(b"truck,state,miles\nT1,KY,100\n")
    assert _archive_is_safe(csv) is True


# ---------------------------------------------------------------------------
# Per-submission file-count cap
# ---------------------------------------------------------------------------


def test_submit_rejects_too_many_files(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IFTA_WEB_MAX_FILES", "2")
    files = [
        ("files", (f"f{i}.csv", b"truck,state,miles\nT1,KY,1\n", "text/csv")) for i in range(3)
    ]
    r = client.post(
        "/submit",
        data={"email": "a@b.co", "quarter": "Q1-2026"},
        files=files,
    )
    assert r.status_code == 400, r.text
    assert "too many files" in r.text


def test_submit_allows_files_at_the_cap(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IFTA_WEB_MAX_FILES", "3")
    files = [
        ("files", (f"f{i}.csv", b"truck,state,miles\nT1,KY,1\n", "text/csv")) for i in range(3)
    ]
    r = client.post(
        "/submit",
        data={"email": "a@b.co", "quarter": "Q1-2026"},
        files=files,
    )
    assert r.status_code == 202, r.text


# ---------------------------------------------------------------------------
# Turnstile fail-closed when mandated
# ---------------------------------------------------------------------------


def test_submit_fails_closed_when_turnstile_required_but_unset(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IFTA_WEB_REQUIRE_TURNSTILE", "1")  # no secret configured
    r = client.post(
        "/submit",
        data={"email": "a@b.co", "quarter": "Q1-2026"},
        files={"mileage_file": ("m.csv", b"x", "text/csv")},
    )
    assert r.status_code == 503, r.text


def test_submit_open_in_dev_without_turnstile(client: TestClient) -> None:
    """Default dev behaviour (no secret, not required) still accepts uploads."""
    r = client.post(
        "/submit",
        data={"email": "a@b.co", "quarter": "Q1-2026"},
        files={"mileage_file": ("m.csv", b"truck,state,miles\nT1,KY,1\n", "text/csv")},
    )
    assert r.status_code == 202, r.text


# ---------------------------------------------------------------------------
# Constant-time backend-key comparison
# ---------------------------------------------------------------------------


def test_valid_backend_key_bypasses_captcha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dev_env(tmp_path, monkeypatch)
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "ts_secret")  # CAPTCHA on
    monkeypatch.setenv("IFTA_WEB_BACKEND_KEY", "backend-key-123")
    from ifta.web.app import create_app

    c = TestClient(create_app())
    r = c.post(
        "/submit",
        headers={"X-Backend-Key": "backend-key-123"},
        data={"email": "a@b.co", "quarter": "Q1-2026"},
        files={"mileage_file": ("m.csv", b"truck,state,miles\nT1,KY,1\n", "text/csv")},
    )
    assert r.status_code == 202, r.text


def test_wrong_backend_key_still_requires_captcha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dev_env(tmp_path, monkeypatch)
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "ts_secret")
    monkeypatch.setenv("IFTA_WEB_BACKEND_KEY", "backend-key-123")
    from ifta.web.app import create_app

    c = TestClient(create_app())
    r = c.post(
        "/submit",
        headers={"X-Backend-Key": "wrong"},
        data={"email": "a@b.co", "quarter": "Q1-2026"},
        files={"mileage_file": ("m.csv", b"x", "text/csv")},
    )
    # Bad key → not authenticated → CAPTCHA enforced; no token → 400.
    assert r.status_code == 400, r.text
    assert "CAPTCHA" in r.text
