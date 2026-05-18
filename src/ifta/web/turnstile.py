"""Cloudflare Turnstile server-side verification.

Turnstile is the no-puzzle, accessibility-friendly CAPTCHA. The frontend
widget produces a one-time token; we POST it to Cloudflare's siteverify
endpoint with our secret key, and Cloudflare tells us whether to trust it.

In dev (no TURNSTILE_SECRET_KEY) the app skips this check entirely.
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger("ifta.web.turnstile")

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def verify_token(
    token: str,
    *,
    secret: str,
    remote_ip: str | None = None,
    timeout: float = 5.0,
) -> bool:
    """Return True if Cloudflare confirms the token.

    Treats any network failure as "not verified" — we'd rather reject a
    legitimate submission than admit a bot during an outage.
    """
    if not token or not secret:
        return False
    data = {"secret": secret, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        r = requests.post(SITEVERIFY_URL, data=data, timeout=timeout)
        r.raise_for_status()
        body = r.json()
    except (requests.RequestException, ValueError):
        log.warning("turnstile verify failed (network/parse error)")
        return False
    if not body.get("success"):
        log.info("turnstile verify rejected: %s", body.get("error-codes"))
        return False
    return True
