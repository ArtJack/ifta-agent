"""Benchmark gate over the receipt-extraction eval.

Turns the ad-hoc eval into a repeatable, *gated*, *tracked* benchmark:

- **Gate** — a run is scored against explicit thresholds (tax-critical fields held
  hardest), and the command exits non-zero on failure, so it can block a bad
  prompt/model change in a script or pre-merge check.
- **Regression** — the run is compared to the previous one; any field that drops
  more than a tolerance is flagged (and fails the gate).
- **Tracked** — each run's scorecard (aggregate numbers, *no* receipt content) is
  appended to a local history so accuracy is followed over time and across models.

The gold set, predictions, and history are customer-derived and stay git-ignored;
this module and its thresholds are PII-free.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The gate. Tax-critical fields (date/state/gallons) are held hardest; a confidently
# wrong one can mis-file a return, so the dangerous-error budget is zero.
DEFAULT_THRESHOLDS: dict[str, Any] = {
    "min_tax_safe_rate": 0.95,
    "max_dangerous": 0,
    "min_field_accuracy": {"date": 0.95, "state": 0.95, "gallons": 0.95},
    "max_regression": 0.03,  # no field may drop more than 3 points vs the baseline
}


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class BenchmarkResult:
    passed: bool
    checks: list[Check]


def _accuracy(report: dict[str, Any], field: str) -> float | None:
    return report["per_field"][field]["accuracy_when_present"]


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def evaluate(report: dict[str, Any], thresholds: dict[str, Any] | None = None) -> BenchmarkResult:
    """Score an aggregate() report against absolute thresholds."""
    t = thresholds or DEFAULT_THRESHOLDS
    checks: list[Check] = []

    tsr = report["summary"]["tax_safe_rate"] or 0.0
    checks.append(
        Check("tax_safe_rate", tsr >= t["min_tax_safe_rate"],
              f"{_pct(tsr)} (min {_pct(t['min_tax_safe_rate'])})")
    )
    danger = report["summary"]["tax_danger_count"]
    checks.append(
        Check("dangerous_tax_errors", danger <= t["max_dangerous"],
              f"{danger} (max {t['max_dangerous']})")
    )
    for field, floor in t["min_field_accuracy"].items():
        acc = _accuracy(report, field)
        checks.append(
            Check(f"{field}_accuracy", acc is not None and acc >= floor,
                  f"{_pct(acc)} (min {_pct(floor)})")
        )
    return BenchmarkResult(passed=all(c.passed for c in checks), checks=checks)


def scorecard(report: dict[str, Any], *, model: str, note: str = "") -> dict[str, Any]:
    """A compact, PII-free record of one run."""
    s = report["summary"]
    return {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": model,
        "note": note,
        "n": s["n_receipts"],
        "tax_safe_rate": s["tax_safe_rate"],
        "dangerous": s["tax_danger_count"],
        "fields": {f: _accuracy(report, f) for f in report["per_field"]},
    }


def compare(current: dict[str, Any], baseline: dict[str, Any], *, max_regression: float = 0.03) -> dict[str, Any]:
    """Per-field accuracy delta between two scorecards; flag fields that dropped too far."""
    deltas: dict[str, float] = {}
    regressions: list[dict[str, Any]] = []
    for field, cur in current["fields"].items():
        base = baseline["fields"].get(field)
        if cur is None or base is None:
            continue
        delta = cur - base
        deltas[field] = delta
        if delta < -max_regression:
            regressions.append({"field": field, "from": base, "to": cur, "delta": delta})
    return {"deltas": deltas, "regressions": regressions}


def append_history(card: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(card) + "\n")


def load_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
