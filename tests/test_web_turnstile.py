"""Tests for the Turnstile verifier — monkeypatches requests.post."""

from __future__ import annotations

from typing import Any

import pytest
import requests

from ifta.web import turnstile


class _FakeResponse:
    def __init__(self, body: dict[str, Any], status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._body


def test_verify_token_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, data: dict[str, str], timeout: float) -> _FakeResponse:
        captured["url"] = url
        captured["data"] = data
        return _FakeResponse({"success": True})

    monkeypatch.setattr(requests, "post", fake_post)
    assert turnstile.verify_token("tok", secret="sec", remote_ip="1.2.3.4") is True
    assert captured["url"] == turnstile.SITEVERIFY_URL
    assert captured["data"]["secret"] == "sec"
    assert captured["data"]["response"] == "tok"
    assert captured["data"]["remoteip"] == "1.2.3.4"


def test_verify_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        requests,
        "post",
        lambda *a, **kw: _FakeResponse(
            {"success": False, "error-codes": ["invalid-input-response"]}
        ),
    )
    assert turnstile.verify_token("bad", secret="sec") is False


def test_verify_token_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **kw: Any) -> Any:
        raise requests.ConnectionError("DNS fail")

    monkeypatch.setattr(requests, "post", boom)
    assert turnstile.verify_token("tok", secret="sec") is False


def test_verify_token_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        requests, "post", lambda *a, **kw: _FakeResponse({}, status_code=500)
    )
    assert turnstile.verify_token("tok", secret="sec") is False


def test_verify_token_empty_inputs_short_circuit() -> None:
    assert turnstile.verify_token("", secret="sec") is False
    assert turnstile.verify_token("tok", secret="") is False
