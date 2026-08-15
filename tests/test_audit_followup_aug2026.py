"""Regressions for the 2026-08-11 audit findings.

Each test here failed before its fix. Grouped by finding so a future reader can
map a test back to the defect it pins.
"""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from ifta.backup import split_dsn_password
from ifta.validator import Finding
from ifta.web import db, worker
from ifta.web.customer_view import render_customer_view, render_customer_view_html
from ifta.web.models import SubmissionStatus

ROOT = Path(__file__).resolve().parents[1]


def _ret(**kw):
    base = {
        "total_tax_due": 1234.56,
        "fleet_mpg": 2.1,
        "rate_warning": None,
        "rate_fallback_used": False,
        "quarter": "Q1-2026",
    }
    return SimpleNamespace(**{**base, **kw})


def _sub(**kw):
    base = {"quarter": "Q1-2026", "name": "Eugene", "email": "x@y.com"}
    return SimpleNamespace(**{**base, **kw})


BLOCKING = [
    Finding(
        code="MPG_LOW",
        severity="error",
        message="Fleet MPG 2.10 is below the realistic floor of 3.0.",
    )
]


# ─── finding 1: the packet email contradicted its own filing gate ─────────────


def test_blocked_packet_email_does_not_open_with_is_ready() -> None:
    """The lead sentence used to be unconditional, so a DO_NOT_FILE return read
    'Your Q1-2026 IFTA packet is ready. We found issues — please don't file
    yet.' The first sentence is the one a customer acts on."""
    body = render_customer_view(sub=_sub(), ret=_ret(), note=None, findings=BLOCKING, truck_count=1)
    lead = body.splitlines()[2]
    assert "is ready" not in lead, lead
    assert "needs attention before you file" in lead, lead


def test_blocked_packet_html_does_not_open_with_is_ready() -> None:
    html = render_customer_view_html(
        sub=_sub(), ret=_ret(), note=None, findings=BLOCKING, truck_count=1
    )
    assert "packet is ready" not in html
    assert "needs attention before you file" in html


def test_ready_packet_still_says_ready() -> None:
    """The fix must not flip the wording for a genuinely clean return."""
    body = render_customer_view(sub=_sub(), ret=_ret(), note=None, findings=[], truck_count=1)
    assert "Your Q1-2026 IFTA packet is ready." in body


def test_blocked_packet_action_header_does_not_assume_filing() -> None:
    body = render_customer_view(sub=_sub(), ret=_ret(), note=None, findings=BLOCKING, truck_count=1)
    assert "Please resolve these before filing:" in body
    assert "Before you file, please double-check:" not in body


# ─── finding 2: install.sh syntax-errored on its own documented .env ──────────


def _env_value_helper() -> str:
    """Lift the env_value() function out of install.sh so it can be exercised."""
    text = (ROOT / "deploy" / "oracle" / "install.sh").read_text()
    match = re.search(r"^env_value\(\) \{.*?^\}", text, re.S | re.M)
    assert match, "env_value() not found in install.sh"
    return match.group(0)


def test_installer_never_shell_sources_the_env_file() -> None:
    """`source`ing operator config is what broke it: compose's .env format is
    not bash, so `RESEND_FROM_EMAIL=ArtJeck IFTA <ifta@artjeck.com>` is a
    redirect and a syntax error."""
    text = (ROOT / "deploy" / "oracle" / "install.sh").read_text()
    assert 'source "$ENV_FILE"' not in text
    assert '. "$ENV_FILE"' not in text


def test_installer_parses_values_with_spaces_and_angle_brackets(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "IFTA_STATE_DIR=/srv/ifta\n"
        "RESEND_FROM_EMAIL=ArtJeck IFTA <ifta@artjeck.com>\n"
        "POSTGRES_PASSWORD=p@ss w0rd/with=signs\n"
    )
    script = f'set -euo pipefail\nENV_FILE="{env_file}"\n{_env_value_helper()}\n'
    script += (
        'printf "%s\\n" "$(env_value IFTA_STATE_DIR)" '
        '"$(env_value RESEND_FROM_EMAIL)" "$(env_value POSTGRES_PASSWORD)" '
        '"$(env_value ABSENT)"\n'
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "/srv/ifta",
        "ArtJeck IFTA <ifta@artjeck.com>",
        "p@ss w0rd/with=signs",
        "",
    ]


# ─── finding 3: all three retries burned in milliseconds ─────────────────────


def _queued(tmp_path: Path):
    db_path = tmp_path / "jobs.db"
    db.init_db(db_path)
    db.create_submission(
        db_path,
        submission_id="s1",
        email="a@b.com",
        quarter="Q1-2026",
        confirm_token="t1",
    )
    db.confirm_submission(db_path, "t1")
    return db_path


def test_requeue_sets_a_future_next_attempt_at(tmp_path: Path) -> None:
    db_path = _queued(tmp_path)
    db.claim_next_queued(db_path)
    before = datetime.now(UTC)

    requeued = db.requeue_for_retry(db_path, "s1", error="boom", delay_seconds=60)

    assert requeued is not None
    assert requeued.next_attempt_at is not None
    assert requeued.next_attempt_at > before + timedelta(seconds=30)


def test_claim_skips_a_job_still_inside_its_backoff(tmp_path: Path) -> None:
    db_path = _queued(tmp_path)
    db.claim_next_queued(db_path)
    db.requeue_for_retry(db_path, "s1", error="boom", delay_seconds=300)

    assert db.claim_next_queued(db_path) is None, "job re-claimed during its back-off"


def test_claim_returns_the_job_once_the_backoff_has_passed(tmp_path: Path) -> None:
    db_path = _queued(tmp_path)
    db.claim_next_queued(db_path)
    db.requeue_for_retry(db_path, "s1", error="boom", delay_seconds=-1)

    claimed = db.claim_next_queued(db_path)
    assert claimed is not None and claimed.id == "s1"


def test_transient_failure_does_not_burn_every_attempt_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: a requeued row is immediately the oldest QUEUED one, so
    the worker re-claimed it in the same millisecond. Three attempts and the
    customer's failure email were spent in ~7ms — long before an iftach.org
    blip could clear."""
    db_path = _queued(tmp_path)
    seen: list[int] = []
    emails: list[str] = []

    def boom(_dir, sub):
        seen.append(sub.attempts)
        raise RuntimeError("iftach.org unreachable")

    monkeypatch.setattr(worker, "process_submission", boom)

    for _ in range(5):
        if (
            worker.process_one_job(db_path, tmp_path, on_failure=lambda s, e: emails.append(s.id))
            is None
        ):
            break

    assert seen == [1], f"expected one attempt before backing off, got {seen}"
    assert emails == [], "customer emailed about a fault that had not been retried yet"
    assert db.get_submission(db_path, "s1").status is SubmissionStatus.QUEUED


def test_backoff_grows_per_attempt() -> None:
    assert worker.retry_delay_seconds(1, base=60) == 60
    assert worker.retry_delay_seconds(2, base=60) == 120
    assert worker.retry_delay_seconds(3, base=60) == 240


# ─── finding 4: /submit failed open when Turnstile was unconfigured ──────────


def test_oracle_env_example_requires_turnstile() -> None:
    text = (ROOT / "deploy" / "oracle" / ".env.example").read_text()
    assert "IFTA_WEB_REQUIRE_TURNSTILE=1" in text


def test_compose_defaults_require_turnstile_on() -> None:
    """Defaulted ON so an .env predating the variable still fails closed."""
    text = (ROOT / "deploy" / "oracle" / "docker-compose.yml").read_text()
    assert "IFTA_WEB_REQUIRE_TURNSTILE: ${IFTA_WEB_REQUIRE_TURNSTILE:-1}" in text


# ─── finding 6: the hermetic penny-accurate fixture must stay tracked ────────


def test_synthetic_golden_fixture_is_present() -> None:
    """The real-carrier regressions skip without their untracked PII inputs, so
    this synthetic quarter is the only penny-accurate check that runs on a
    clean checkout. It must not be swept up by the `inbox/*/` ignore rule."""
    inbox = ROOT / "inbox" / "Q2-2026"
    assert inbox.is_dir(), "synthetic golden fixture is missing"
    assert (ROOT / "data" / "rates" / "2Q2026.csv").exists()
    tracked = subprocess.run(
        ["git", "check-ignore", str(inbox)], cwd=ROOT, capture_output=True, text=True
    )
    assert tracked.returncode != 0, "inbox/Q2-2026 is git-ignored — it would be lost"


# ─── finding 7: the DB password travelled in pg_dump's argv ──────────────────


@pytest.mark.parametrize(
    ("dsn", "expected_dsn", "expected_pw"),
    [
        (
            "postgresql://ifta:p%40ss%2Fw0rd@postgres:5432/ifta",
            "postgresql://ifta@postgres:5432/ifta",
            "p@ss/w0rd",
        ),
        (
            "postgresql://ifta:plain@host:5432/db?sslmode=require",
            "postgresql://ifta@host:5432/db?sslmode=require",
            "plain",
        ),
        ("postgresql://ifta@host/db", "postgresql://ifta@host/db", None),
        ("postgresql:///db", "postgresql:///db", None),
    ],
)
def test_split_dsn_password(dsn: str, expected_dsn: str, expected_pw: str | None) -> None:
    assert split_dsn_password(dsn) == (expected_dsn, expected_pw)


def test_pg_dump_gets_no_password_in_argv(tmp_path: Path, monkeypatch) -> None:
    """argv is world-readable via /proc, and TimeoutExpired embeds the whole
    command in its message — so a traceback used to print the password."""
    from ifta import backup as backup_mod

    root = tmp_path / "proj"
    (root / "data").mkdir(parents=True)
    (root / "data" / "x.txt").write_text("hi")
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env") or {}
        dest = next(a.split("=", 1)[1] for a in cmd if a.startswith("--file="))
        Path(dest).write_bytes(b"PGDMP-fake")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(backup_mod.subprocess, "run", fake_run)
    backup_mod.backup_data(
        root, tmp_path / "b", db_url="postgresql://ifta:SuperSecret@postgres:5432/ifta"
    )

    assert not any("SuperSecret" in str(a) for a in captured["cmd"]), captured["cmd"]
    assert captured["env"].get("PGPASSWORD") == "SuperSecret"


def test_pg_dump_timeout_message_does_not_carry_the_password(tmp_path: Path, monkeypatch) -> None:
    from ifta import backup as backup_mod

    root = tmp_path / "proj"
    (root / "data").mkdir(parents=True)
    (root / "data" / "x.txt").write_text("hi")

    def timeout_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 900)

    monkeypatch.setattr(backup_mod.subprocess, "run", timeout_run)

    with pytest.raises(RuntimeError) as exc:
        backup_mod.backup_data(
            root, tmp_path / "b", db_url="postgresql://ifta:SuperSecret@postgres:5432/ifta"
        )

    chain: list[str] = []
    err: BaseException | None = exc.value
    while err is not None:
        chain.append(str(err))
        err = err.__cause__ or err.__context__
    assert not any("SuperSecret" in part for part in chain), chain
