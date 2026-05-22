"""FastAPI app for IFTA web intake.

Exposes:
- GET  /healthz       — readiness probe
- GET  /status/{id}   — JSON status for a submission
- POST /submit        — accept multipart upload, save files, create row

The form lives on artjeck.com; this is the cross-origin API it POSTs to.
Run with: `ifta web` (uvicorn factory mode).
"""

from __future__ import annotations

import contextlib
import html
import logging
import os
import re
import secrets
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exception_handlers import (
    http_exception_handler as _default_http_handler,
)
from fastapi.exception_handlers import (
    request_validation_exception_handler as _default_validation_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.exceptions import HTTPException as StarletteHTTPException

from ifta.client import quarter_key
from ifta.notify import AdminNotifier, format_event, load_admin_notifier_config
from ifta.web import db
from ifta.web.email import EmailClient, load_email_config_from_env
from ifta.web.models import SubmissionStatus
from ifta.web.turnstile import verify_token as verify_turnstile_token

log = logging.getLogger("ifta.web.app")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".pdf"}
DEFAULT_MAX_FILE_MB = 10
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def get_db_path() -> Path:
    env_val = os.environ.get("IFTA_WEB_DB_PATH")
    return Path(env_val) if env_val else PROJECT_ROOT / "data" / "web_jobs.db"


def get_submissions_dir() -> Path:
    env_val = os.environ.get("IFTA_WEB_SUBMISSIONS_DIR")
    return Path(env_val) if env_val else PROJECT_ROOT / "data" / "web_submissions"


def get_max_file_bytes() -> int:
    mb = int(os.environ.get("IFTA_WEB_MAX_FILE_MB", str(DEFAULT_MAX_FILE_MB)))
    return mb * 1024 * 1024


def _cors_origins() -> list[str]:
    raw = os.environ.get(
        "IFTA_WEB_CORS_ORIGINS", "https://artjeck.com,http://localhost:3000"
    )
    return [o.strip() for o in raw.split(",") if o.strip()]


def _submit_rate_limit() -> str:
    return os.environ.get("IFTA_WEB_SUBMIT_RATE_LIMIT", "3/hour")


def _turnstile_secret() -> str | None:
    return os.environ.get("TURNSTILE_SECRET_KEY") or None


def create_app() -> FastAPI:
    """Factory used by uvicorn (`ifta.web.app:create_app --factory`).

    Reads env vars at call time, so tests can monkeypatch env then call
    create_app() to get a fresh, isolated instance.
    """
    app = FastAPI(title="IFTA Intake API")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # In-memory rate limiter — fine for a single Mac-mini worker. If we ever
    # run two web processes behind a load balancer this should point at Redis.
    rate_limit_str = _submit_rate_limit()
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
    # The access log records only the status line, never the reason. These
    # log the failing fields (422) and the HTTPException detail (4xx) before
    # delegating to FastAPI's default responses — so 400/422 stop being opaque.
    # Field names and static detail strings only; no submitted values.
    app.add_exception_handler(RequestValidationError, _logging_validation_handler)
    app.add_exception_handler(StarletteHTTPException, _logging_http_handler)
    app.add_middleware(SlowAPIMiddleware)

    db_path = get_db_path()
    submissions_dir = get_submissions_dir()
    max_file_bytes = get_max_file_bytes()
    email_config = load_email_config_from_env()
    email_client = EmailClient(email_config)
    notifier = AdminNotifier(load_admin_notifier_config())
    turnstile_secret = _turnstile_secret()

    db.init_db(db_path)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/status/{submission_id}")
    def status(submission_id: str) -> dict[str, str | bool | None]:
        sub = db.get_submission(db_path, submission_id)
        if sub is None:
            raise HTTPException(status_code=404, detail="submission not found")
        return {
            "submission_id": sub.id,
            "status": sub.status.value,
            "quarter": sub.quarter,
            "error": sub.error,
            # packet_sent_at lets ops distinguish "DONE and emailed" from
            # "DONE but Resend send failed silently". NULL = packet not (yet) sent.
            "packet_sent": sub.packet_sent_at.isoformat() if sub.packet_sent_at else None,
        }

    @app.post("/submit", status_code=202)
    @limiter.limit(rate_limit_str)
    async def submit(
        request: Request,
        email: str = Form(...),
        quarter: str = Form(...),
        company: str | None = Form(None),
        # FastAPI Form binding is verbatim — no hyphen↔underscore mapping.
        # Cloudflare's widget emits a hidden input literally named
        # `cf-turnstile-response`, so the alias is required.
        cf_turnstile_response: str | None = Form(None, alias="cf-turnstile-response"),
        mileage_file: UploadFile = File(...),
        fuel_file: UploadFile = File(...),
    ) -> dict[str, str]:
        email = (email or "").strip()
        if not EMAIL_RE.match(email):
            raise HTTPException(status_code=400, detail="invalid email")
        try:
            qkey = quarter_key(quarter)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        if turnstile_secret:
            if not cf_turnstile_response:
                raise HTTPException(status_code=400, detail="missing CAPTCHA token")
            remote_ip = request.client.host if request.client else None
            if not verify_turnstile_token(
                cf_turnstile_response,
                secret=turnstile_secret,
                remote_ip=remote_ip,
            ):
                raise HTTPException(status_code=400, detail="CAPTCHA verification failed")

        _validate_upload(mileage_file)
        _validate_upload(fuel_file)

        sid = uuid.uuid4().hex
        token = secrets.token_urlsafe(32)
        sub_root = submissions_dir / sid

        # With email enabled, customer must click the link to start processing.
        # In dev (no API key) we skip confirmation so manual testing works.
        initial_status = (
            SubmissionStatus.PENDING_CONFIRMATION
            if email_config.enabled
            else SubmissionStatus.QUEUED
        )

        # Either every artifact lands (files + DB row) or nothing does. A 413
        # on the second upload, or a UNIQUE collision on confirm_token, would
        # otherwise leave the first file behind forever.
        try:
            inbox = sub_root / "inbox" / qkey
            inbox.mkdir(parents=True, exist_ok=True)
            _save_upload(mileage_file, inbox, max_file_bytes, prefix="mileage")
            _save_upload(fuel_file, inbox, max_file_bytes, prefix="fuel")
            sub = db.create_submission(
                db_path,
                submission_id=sid,
                email=email,
                quarter=qkey,
                confirm_token=token,
                company=(company or "").strip() or None,
                status=initial_status,
            )
        except Exception:
            shutil.rmtree(sub_root, ignore_errors=True)
            raise

        if email_config.enabled and not email_client.send_confirmation(sub):
                # Don't strand the customer in PENDING_CONFIRMATION with no
                # way out. Mark FAILED so /status surfaces it and ops can
                # see what happened.
                db.mark_failed(
                    db_path,
                    sub.id,
                    error=(
                        "Couldn't send the confirmation email. "
                        "Try again in a few minutes or email hello@artjeck.com."
                    ),
                )
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "Couldn't send confirmation email. "
                        "Please try again in a few minutes."
                    ),
                )
        return {"submission_id": sub.id, "status": sub.status.value}

    @app.get("/confirm/{token}", response_class=HTMLResponse)
    def confirm(token: str) -> HTMLResponse:
        sub = db.confirm_submission(db_path, token)
        if sub is None:
            return HTMLResponse(_html_page("Link not found", _NOT_FOUND_HTML), status_code=404)
        if sub.status == SubmissionStatus.QUEUED:
            # Never let a notification failure block the customer's UX.
            with contextlib.suppress(Exception):
                notifier.send(
                    format_event(
                        headline="🟢 IFTA submission confirmed — queued",
                        source="web intake",
                        customer=sub.email,
                        quarter=sub.quarter,
                        extras={
                            "Company": sub.company or "—",
                            "Submission": sub.id,
                        },
                    )
                )
            body = _confirmed_html(sub.email, sub.quarter)
            return HTMLResponse(_html_page("Processing started", body))
        if sub.status in (SubmissionStatus.RUNNING, SubmissionStatus.DONE):
            body = _already_running_html(sub.email, sub.quarter, sub.status)
            return HTMLResponse(_html_page("Already processing", body))
        if sub.status == SubmissionStatus.FAILED:
            return HTMLResponse(_html_page("Submission failed", _FAILED_HTML), status_code=200)
        # PENDING_CONFIRMATION shouldn't happen after confirm_submission, but
        # surface it cleanly if it does (e.g. concurrent races).
        return HTMLResponse(_html_page("Unexpected state", _GENERIC_HTML), status_code=500)

    return app


def _validate_upload(f: UploadFile) -> None:
    name = f.filename or ""
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unsupported file type: {ext or '(no extension)'} "
                "— use .csv, .xlsx, .xls, or .pdf"
            ),
        )


def _rate_limit_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Friendlier 429 body than slowapi's default plain-text response."""
    assert isinstance(exc, RateLimitExceeded)
    return JSONResponse(
        status_code=429,
        content={
            "detail": (
                "Too many submissions from this IP. "
                f"Limit: {exc.detail}. Try again later."
            )
        },
    )


async def _logging_validation_handler(request: Request, exc: Exception) -> Response:
    """Log which fields failed validation (the 422s), then delegate to default."""
    assert isinstance(exc, RequestValidationError)
    fields = sorted(
        {
            ".".join(str(p) for p in e.get("loc", ()) if p != "body")
            for e in exc.errors()
        }
    )
    log.warning(
        "422 %s %s — invalid/missing fields: %s",
        request.method,
        request.url.path,
        ", ".join(f for f in fields if f) or "(none)",
    )
    return await _default_validation_handler(request, exc)


async def _logging_http_handler(request: Request, exc: Exception) -> Response:
    """Log the HTTPException detail for 4xx/5xx, then delegate to default.

    404s log at INFO (probes/favicon are routine noise); everything else 4xx+
    at WARNING so /submit rejections surface their reason.
    """
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code >= 400:
        level = logging.INFO if exc.status_code == 404 else logging.WARNING
        log.log(
            level,
            "%d %s %s — %s",
            exc.status_code,
            request.method,
            request.url.path,
            exc.detail,
        )
    return await _default_http_handler(request, exc)


def _html_page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html><head>"
        f"<title>{title} — ArtJeck IFTA</title>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<style>"
        "body{font-family:system-ui,-apple-system,sans-serif;max-width:560px;"
        "margin:6rem auto;padding:0 1.5rem;color:#1a1a1a;line-height:1.6}"
        "h1{color:#0a8a3a;margin-bottom:1rem}p{color:#555}code{background:#f0f0f0;"
        "padding:0.15em 0.4em;border-radius:3px}"
        "</style></head>"
        f"<body>{body}</body></html>"
    )


def _confirmed_html(email: str, quarter: str) -> str:
    # Both values are escaped — EMAIL_RE permits HTML-active chars (e.g.
    # `<svg/onload=…>@a.b` passes), so the only protection here is escaping
    # at render time.
    safe_email = html.escape(email)
    safe_quarter = html.escape(quarter)
    return (
        f"<h1>Got it — processing started</h1>"
        f"<p>You'll receive your <strong>{safe_quarter}</strong> IFTA packet at "
        f"<code>{safe_email}</code> in about 5 minutes.</p>"
        f"<p>The email will include your portal CSV, a review note, and one "
        f"Excel file per truck.</p>"
    )


def _already_running_html(email: str, quarter: str, status: SubmissionStatus) -> str:
    label = (
        "is already processing" if status == SubmissionStatus.RUNNING else "is ready"
    )
    safe_email = html.escape(email)
    safe_quarter = html.escape(quarter)
    return (
        f"<h1>Already confirmed</h1>"
        f"<p>Your <strong>{safe_quarter}</strong> submission {label}. "
        f"Check <code>{safe_email}</code> — the packet should arrive shortly "
        f"(or has already arrived).</p>"
    )


_NOT_FOUND_HTML = (
    "<h1>Link not found</h1>"
    "<p>This confirmation link is invalid or has been deleted. "
    "If you uploaded files recently, re-submit at "
    "<a href='https://artjeck.com/ifta'>artjeck.com/ifta</a>.</p>"
)

_FAILED_HTML = (
    "<h1>Couldn't process your files</h1>"
    "<p>The pipeline failed on this submission. Check your inbox for an "
    "email explaining what went wrong, or contact "
    "<a href='mailto:hello@artjeck.com'>hello@artjeck.com</a>.</p>"
)

_GENERIC_HTML = (
    "<h1>Something went wrong</h1>"
    "<p>If this persists, email <a href='mailto:hello@artjeck.com'>"
    "hello@artjeck.com</a>.</p>"
)


def _save_upload(f: UploadFile, dest_dir: Path, max_bytes: int, *, prefix: str) -> Path:
    """Save an uploaded file with a field-name prefix to avoid collisions.

    Two real customer files can share a name (e.g. both `export.csv` from
    the same fleet portal, or both `filename=""` so they fall back to
    `upload`). Without a prefix the second save would silently overwrite
    the first and the pipeline would compute on partial data.
    """
    raw_name = SAFE_FILENAME_RE.sub("_", Path(f.filename or "upload").name)
    safe_name = f"{prefix}_{raw_name}"
    dest = dest_dir / safe_name
    written = 0
    with dest.open("wb") as out:
        while chunk := f.file.read(64 * 1024):
            written += len(chunk)
            if written > max_bytes:
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"{f.filename} exceeds {max_bytes // (1024 * 1024)} MB limit",
                )
            out.write(chunk)
    return dest
