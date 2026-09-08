"""Filing-day QA checks for high-risk IFTA operations."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import requests

from ifta.backup import backup_dir as resolve_backup_dir
from ifta.backup import list_snapshots
from ifta.rates import fetch_rates

CheckStatus = Literal["PASS", "WARN", "FAIL", "SKIP"]

_ERROR_PATTERNS = re.compile(r"\b(ERROR|CRITICAL|Traceback|Exception)\b", re.IGNORECASE)


@dataclass
class QaCheck:
    name: str
    status: CheckStatus
    detail: str


@dataclass
class FilingDayQaReport:
    quarter: str
    client: str | None
    generated_at: str
    checks: list[QaCheck]
    report_path: Path | None = None

    @property
    def verdict(self) -> CheckStatus:
        statuses = {check.status for check in self.checks}
        if "FAIL" in statuses:
            return "FAIL"
        if "WARN" in statuses:
            return "WARN"
        if "SKIP" in statuses:
            return "WARN"
        return "PASS"


def check_health(url: str, *, timeout: int = 10, label: str = "health") -> QaCheck:
    try:
        resp = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        return QaCheck(label, "FAIL", f"{url} unavailable: {exc}")
    if resp.status_code != 200:
        return QaCheck(label, "FAIL", f"{url} returned HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        return QaCheck(label, "WARN", f"{url} returned 200 but non-JSON body")
    if body.get("status") == "ok":
        return QaCheck(label, "PASS", f"{url} status=ok")
    return QaCheck(label, "WARN", f"{url} returned 200 with unexpected body: {body!r}")


def check_latest_backup(
    *, max_age_hours: float = 24.0, backup_dir: Path | None = None
) -> QaCheck:
    """Is there a recent snapshot?

    Distinguishes two very different answers. "The backup directory is not
    reachable from this machine" is a WARN — production snapshots live on the
    server, and running this from a laptop proves nothing either way. "The
    directory is right here and holds nothing, or nothing recent" is a FAIL,
    because that is the state where a bad filing cannot be rolled back.
    """
    dest = resolve_backup_dir(backup_dir)
    if not dest.exists():
        return QaCheck(
            "backup recency",
            "WARN",
            f"{dest} not reachable from this host — check on the server "
            "(`ls /var/lib/ifta/backups`) or pass --backup-dir",
        )
    snapshots = list_snapshots(backup_dir)
    if not snapshots:
        return QaCheck("backup recency", "FAIL", f"no backup snapshots in {dest}")
    latest = snapshots[-1]
    age_hours = (
        datetime.now(UTC) - datetime.fromtimestamp(latest.stat().st_mtime, UTC)
    ).total_seconds() / 3600
    detail = f"latest {latest.name}, age {age_hours:.1f}h"
    if age_hours <= max_age_hours:
        return QaCheck("backup recency", "PASS", detail)
    return QaCheck("backup recency", "FAIL", f"{detail}; exceeds {max_age_hours:.1f}h")


def check_rates_current(quarter: str, *, fuel: str = "diesel", force: bool = False) -> QaCheck:
    try:
        rates = fetch_rates(quarter, fuel=fuel, force=force)
    except Exception as exc:
        return QaCheck("current-quarter rates", "FAIL", f"could not load rates: {exc}")
    if rates.fallback_used:
        source = rates.source_quarter or rates.quarter
        return QaCheck(
            "current-quarter rates",
            "FAIL",
            f"{quarter} rates fell back to {source}; do not file",
        )
    return QaCheck(
        "current-quarter rates",
        "PASS",
        f"{rates.quarter} {rates.fuel}: {len(rates.rates)} jurisdictions",
    )


def check_recent_logs(
    log_paths: list[Path], *, max_lines: int = 80, max_age_hours: float = 48.0
) -> QaCheck:
    """Scan recent log lines for errors.

    Staleness is reported, not ignored. A log last written days ago says nothing
    about whether the service is healthy now — and silently counting it as a
    clean scan is how "all checks passed" comes to mean "we looked at a machine
    that stopped serving traffic weeks ago".
    """
    hits: list[str] = []
    missing: list[str] = []
    stale: list[str] = []
    for path in log_paths:
        if not path.exists():
            missing.append(str(path))
            continue
        age_hours = (
            datetime.now(UTC) - datetime.fromtimestamp(path.stat().st_mtime, UTC)
        ).total_seconds() / 3600
        if age_hours > max_age_hours:
            stale.append(f"{path.name} ({age_hours / 24:.0f}d old)")
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
        for line in lines:
            if _ERROR_PATTERNS.search(line):
                hits.append(f"{path.name}: {line[:180]}")
                break
    if hits:
        return QaCheck("recent logs", "WARN", "; ".join(hits))
    if stale:
        return QaCheck(
            "recent logs",
            "WARN",
            f"only stale logs here ({', '.join(stale)}) — read the server's instead: "
            "`journalctl -u ifta` or `docker compose logs worker`",
        )
    if missing and len(missing) == len(log_paths):
        return QaCheck(
            "recent logs",
            "WARN",
            "no local log files — on the production box use `journalctl -u ifta` "
            "or `docker compose logs`",
        )
    detail = f"checked {len(log_paths) - len(missing)} log file(s)"
    if missing:
        detail += f"; missing {len(missing)}"
    return QaCheck("recent logs", "PASS", detail)


def run_pytest_smoke(project_root: Path, tests: list[str], *, timeout: int = 120) -> QaCheck:
    cmd = [str(project_root / ".venv" / "bin" / "pytest"), *tests]
    if not Path(cmd[0]).exists():
        return QaCheck("targeted smoke tests", "FAIL", f"pytest not found: {cmd[0]}")
    try:
        proc = subprocess.run(
            cmd,
            cwd=project_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return QaCheck("targeted smoke tests", "FAIL", f"timed out after {timeout}s")
    output = (proc.stdout or "").strip().splitlines()
    tail = " | ".join(output[-3:]) if output else "no output"
    if proc.returncode == 0:
        return QaCheck("targeted smoke tests", "PASS", tail)
    return QaCheck("targeted smoke tests", "FAIL", tail)


def run_filing_day_qa(
    *,
    project_root: Path,
    quarter: str,
    client: str | None = None,
    fuel: str = "diesel",
    local_url: str | None = None,
    public_url: str | None = "https://ifta-api.artjeck.com/healthz",
    backup_dir: Path | None = None,
    max_backup_age_hours: float = 24.0,
    skip_public: bool = False,
    skip_tests: bool = False,
    force_rates: bool = False,
    pytest_timeout: int = 120,
) -> FilingDayQaReport:
    # The public URL is the real customer-facing check. A local one only means
    # something when this runs on the server (or in the container), so it is
    # opt-in rather than a check that fails everywhere else for no reason.
    checks: list[QaCheck] = []
    if local_url:
        checks.append(check_health(local_url, label="local health"))
    else:
        checks.append(
            QaCheck("local health", "SKIP", "no --local-url given (run on the server to use it)")
        )
    if skip_public or public_url is None:
        checks.append(QaCheck("public health", "SKIP", "public health check skipped"))
    else:
        checks.append(check_health(public_url, label="public health"))

    checks.append(check_latest_backup(max_age_hours=max_backup_age_hours, backup_dir=backup_dir))
    checks.append(check_rates_current(quarter, fuel=fuel, force=force_rates))
    checks.append(
        check_recent_logs(
            [
                project_root / "logs" / "worker.err.log",
                project_root / "logs" / "web.err.log",
            ]
        )
    )
    if skip_tests:
        checks.append(QaCheck("targeted smoke tests", "SKIP", "pytest smoke skipped"))
    else:
        checks.append(
            run_pytest_smoke(
                project_root,
                [
                    "tests/test_q1_2025.py",
                    "tests/test_q4_2025_menshikov.py",
                    "tests/test_portal_csv.py",
                    "tests/test_review_packet.py",
                    "tests/test_web_pipeline.py",
                    "tests/test_web_worker.py",
                    "tests/test_telegram_bot.py",
                ],
                timeout=pytest_timeout,
            )
        )

    return FilingDayQaReport(
        quarter=quarter,
        client=client,
        generated_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
        checks=checks,
    )


def render_report_md(report: FilingDayQaReport) -> str:
    lines = [
        f"# IFTA Filing-Day QA Report: {report.quarter}",
        "",
        f"- Generated: {report.generated_at}",
        f"- Client: {report.client or 'all/default'}",
        f"- QA verdict: `{report.verdict}`",
        "",
        "## Checks",
        "",
        "| Check | Status | Detail |",
        "|---|---:|---|",
    ]
    for check in report.checks:
        detail = check.detail.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {check.name} | `{check.status}` | {detail} |")
    lines.extend(
        [
            "",
            "## Gate Rule",
            "",
            "- `PASS`: filing-day QA checks passed.",
            "- `WARN`: review warnings or skipped checks before filing.",
            "- `FAIL`: do not file until failed checks are resolved.",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(report: FilingDayQaReport, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    client = re.sub(r"[^a-zA-Z0-9_-]+", "-", report.client or "default").strip("-")
    path = out_dir / f"{stamp}-{report.quarter}-{client}-filing-day-qa.md"
    path.write_text(render_report_md(report), encoding="utf-8")
    report.report_path = path
    return path
