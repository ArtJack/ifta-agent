from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from click.testing import CliRunner

from ifta import qa
from ifta.cli import main
from ifta.rates import RateTable


class _Resp:
    def __init__(self, status_code: int = 200, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {"status": "ok"}

    def json(self) -> dict:
        return self._body


def test_check_health_pass(monkeypatch) -> None:
    monkeypatch.setattr(qa.requests, "get", lambda *_args, **_kwargs: _Resp())

    check = qa.check_health("http://example.test/healthz")

    assert check.status == "PASS"
    assert "status=ok" in check.detail


def test_check_rates_current_fails_on_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        qa,
        "fetch_rates",
        lambda *_args, **_kwargs: RateTable(
            quarter="2Q2026",
            fuel="diesel",
            rates={"CA": 0.79},
            surcharge_rates={},
            source_quarter="1Q2026",
            fallback_used=True,
        ),
    )

    check = qa.check_rates_current("2Q2026")

    assert check.status == "FAIL"
    assert "fell back to 1Q2026" in check.detail


def test_check_latest_backup_passes_when_recent(tmp_path: Path) -> None:
    snap = tmp_path / "ifta-data-20260604T000000Z.tar.gz"
    snap.write_text("x")
    recent = datetime.now(UTC) - timedelta(hours=2)
    ts = recent.timestamp()
    snap.touch()
    import os

    os.utime(snap, (ts, ts))
    # Point at a real directory rather than stubbing the lister: the check now
    # also asks whether the backup location is reachable at all, and stubbing
    # that away is what let a stale local share pass for production's backups.
    check = qa.check_latest_backup(max_age_hours=24, backup_dir=tmp_path)

    assert check.status == "PASS"
    assert snap.name in check.detail


def test_recent_logs_warns_on_traceback(tmp_path: Path) -> None:
    log = tmp_path / "worker.err.log"
    log.write_text("ok\nTraceback (most recent call last):\n")

    check = qa.check_recent_logs([log])

    assert check.status == "WARN"
    assert "Traceback" in check.detail


def test_render_report_includes_verdict() -> None:
    report = qa.FilingDayQaReport(
        quarter="2Q2026",
        client="dm_express",
        generated_at="2026-06-04T00:00:00+00:00",
        checks=[qa.QaCheck("local health", "PASS", "ok")],
    )

    markdown = qa.render_report_md(report)

    assert "QA verdict: `PASS`" in markdown
    assert "| local health | `PASS` | ok |" in markdown


def test_cli_qa_filing_day_writes_report(tmp_path: Path, monkeypatch) -> None:
    def fake_run(**kwargs):
        return qa.FilingDayQaReport(
            quarter=kwargs["quarter"],
            client=kwargs["client"],
            generated_at="2026-06-04T00:00:00+00:00",
            checks=[qa.QaCheck("local health", "PASS", "ok")],
        )

    monkeypatch.setattr(qa, "run_filing_day_qa", fake_run)
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "qa-filing-day",
            "--quarter",
            "Q2-2026",
            "--client",
            "dm_express",
            "--out",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "QA verdict" in result.output
    assert list(tmp_path.glob("*-Q2-2026-dm_express-filing-day-qa.md"))


def test_unreachable_backup_dir_warns_rather_than_failing(tmp_path) -> None:
    """Running this from a laptop proves nothing about the server's backups.

    A FAIL here would cry wolf on every dev-machine run, which is how a real
    backup failure ends up ignored.
    """
    check = qa.check_latest_backup(backup_dir=tmp_path / "definitely-not-here")
    assert check.status == "WARN"
    assert "not reachable" in check.detail


def test_present_but_empty_backup_dir_fails(tmp_path) -> None:
    """Reachable and empty is the dangerous case: no rollback for a bad filing."""
    check = qa.check_latest_backup(backup_dir=tmp_path)
    assert check.status == "FAIL"
    assert "no backup snapshots" in check.detail


def test_local_health_is_skipped_when_no_url_given(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(qa, "check_health", lambda *a, **k: qa.QaCheck("public health", "PASS", ""))
    monkeypatch.setattr(
        qa, "check_rates_current", lambda *a, **k: qa.QaCheck("rates", "PASS", "")
    )
    report = qa.run_filing_day_qa(
        project_root=tmp_path,
        quarter="Q3-2026",
        backup_dir=tmp_path,
        skip_tests=True,
    )
    local = next(c for c in report.checks if c.name == "local health")
    assert local.status == "SKIP"


def test_stale_logs_do_not_count_as_a_clean_scan(tmp_path: Path) -> None:
    """A log nobody has written to in weeks is not evidence the service is well."""
    import os
    import time

    old_log = tmp_path / "worker.err.log"
    old_log.write_text("all quiet\n", encoding="utf-8")
    weeks_ago = time.time() - 21 * 24 * 3600
    os.utime(old_log, (weeks_ago, weeks_ago))

    check = qa.check_recent_logs([old_log])
    assert check.status == "WARN"
    assert "stale" in check.detail
    assert "journalctl" in check.detail, "tell the operator where the real logs are"


def test_fresh_clean_logs_pass(tmp_path: Path) -> None:
    fresh = tmp_path / "web.err.log"
    fresh.write_text("INFO: started\n", encoding="utf-8")
    assert qa.check_recent_logs([fresh]).status == "PASS"
