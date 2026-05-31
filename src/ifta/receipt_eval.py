"""Receipt-extraction eval harness: measure vision accuracy against a human gold set.

The intake pipeline already gates every receipt behind human review. This harness
tells you *how much* you can trust the extractor — field by field — so you can
safely raise the auto-accept bar over time instead of guessing.

You (the human) are the oracle: label receipts blind, run the model, then compare.

Every field comparison lands in one of five outcomes:

    gold has a value   -> CORRECT | WRONG | MISSING
    gold is blank       -> CORRECT_NULL | HALLUCINATION

WRONG and HALLUCINATION on a *tax-critical* field (date, state, gallons) are the
dangerous ones — a confidently wrong gallons can mis-file a return. MISSING is
tolerable: the human catches a blank. The whole point of the harness is to drive
WRONG + HALLUCINATION on the tax-critical fields toward zero, and to find the
confidence threshold above which the extractor is safe to trust.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from ifta.intake.extract import extract_one

Outcome = Literal["CORRECT", "WRONG", "MISSING", "HALLUCINATION", "CORRECT_NULL"]

# Fields that actually move the IFTA tax number. Gate these hardest.
TAX_CRITICAL: tuple[str, ...] = ("date", "state", "gallons")

# Fields worth scoring. address/city/driver are reference-only and noisy, so skip.
SCORED_FIELDS: tuple[str, ...] = (
    "date", "state", "gallons", "amount", "vendor",
    "fuel_type", "truck_id", "card_last4", "invoice", "payment_method",
)

_DANGEROUS: set[Outcome] = {"WRONG", "HALLUCINATION"}
_GOOD: set[Outcome] = {"CORRECT", "CORRECT_NULL"}

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_BLANKS = {"", "null", "none", "n/a", "na", "?", "-"}
_VENDOR_STOPWORDS = {"travel", "center", "centers", "truck", "stop", "stops", "llc", "inc", "the", "co"}
_BUCKETS = [
    ("0.00-0.50", 0.0, 0.5),
    ("0.50-0.80", 0.5, 0.8),
    ("0.80-0.90", 0.8, 0.9),
    ("0.90-1.00", 0.9, 1.01),
]


# ---------------------------------------------------------------------------
# Field-level scoring (pure, the heart of the harness)
# ---------------------------------------------------------------------------


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _BLANKS
    return False


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = _NUM_RE.search(value.replace(",", ""))
        if match:
            return float(match.group())
    return None


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _norm_date(value: Any) -> str:
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def _vendor_match(pred: Any, gold: Any) -> bool:
    def tokens(value: Any) -> set[str]:
        words = re.findall(r"[a-z0-9]+", str(value).lower())
        meaningful = {w for w in words if w not in _VENDOR_STOPWORDS}
        return meaningful or set(words)

    return bool(tokens(pred) & tokens(gold))


def _match(field: str, pred: Any, gold: Any) -> bool:
    if field in {"gallons", "amount"}:
        p, g = _num(pred), _num(gold)
        if p is None or g is None:
            return False
        tolerance = 0.1 if field == "gallons" else 0.02
        return abs(p - g) <= tolerance
    if field == "card_last4":
        return re.sub(r"\D", "", str(pred))[-4:] == re.sub(r"\D", "", str(gold))[-4:]
    if field == "vendor":
        return _vendor_match(pred, gold)
    if field == "date":
        return _norm_date(pred) == _norm_date(gold)
    return _norm(pred) == _norm(gold)


def score_field(field: str, predicted: Any, gold: Any) -> Outcome:
    """Classify one field against the human gold value."""
    gold_blank, pred_blank = _is_blank(gold), _is_blank(predicted)
    if gold_blank and pred_blank:
        return "CORRECT_NULL"
    if gold_blank:
        return "HALLUCINATION"  # model invented a value the receipt does not support
    if pred_blank:
        return "MISSING"  # model abstained; the human review net catches this
    return "CORRECT" if _match(field, predicted, gold) else "WRONG"


def score_candidate(predicted: dict[str, Any], gold: dict[str, Any]) -> dict[str, Outcome]:
    return {f: score_field(f, predicted.get(f), gold.get(f)) for f in SCORED_FIELDS}


# ---------------------------------------------------------------------------
# Aggregation / metrics
# ---------------------------------------------------------------------------


def _safe_div(num: float, denom: float) -> float | None:
    return (num / denom) if denom else None


def _calibration(labels: dict[str, dict], predictions: dict[str, dict]) -> list[dict[str, Any]]:
    """Accuracy of tax-critical fields stratified by the model's own confidence."""
    out: list[dict[str, Any]] = []
    for label, lo, hi in _BUCKETS:
        correct = total = 0
        for name, gold in labels.items():
            confidence = (predictions.get(name, {}) or {}).get("confidence") or {}
            for field in TAX_CRITICAL:
                conf = confidence.get(field)
                if conf is None or _is_blank(gold.get(field)) or not (lo <= float(conf) < hi):
                    continue
                total += 1
                if score_field(field, predictions.get(name, {}).get(field), gold.get(field)) == "CORRECT":
                    correct += 1
        out.append({"bucket": label, "n": total, "accuracy": _safe_div(correct, total)})
    return out


def aggregate(labels: dict[str, dict], predictions: dict[str, dict]) -> dict[str, Any]:
    """Per-field and tax-critical metrics over the whole labeled set."""
    rows = []
    for name, gold in labels.items():
        pred = predictions.get(name, {})
        outcomes = score_candidate(pred, gold)
        rows.append(
            {
                "receipt": name,
                "difficulty": gold.get("_difficulty", "untagged"),
                "outcomes": outcomes,
                "tax_ok": all(outcomes[f] in _GOOD for f in TAX_CRITICAL),
                "tax_danger": any(outcomes[f] in _DANGEROUS for f in TAX_CRITICAL),
                "predicted": name in predictions,
            }
        )

    per_field = {}
    for field in SCORED_FIELDS:
        counts = dict.fromkeys(("CORRECT", "WRONG", "MISSING", "HALLUCINATION", "CORRECT_NULL"), 0)
        for row in rows:
            counts[row["outcomes"][field]] += 1
        present = counts["CORRECT"] + counts["WRONG"] + counts["MISSING"]
        per_field[field] = {
            "counts": counts,
            "accuracy_when_present": _safe_div(counts["CORRECT"], present),
            "danger_rate": _safe_div(counts["WRONG"] + counts["HALLUCINATION"], len(rows) or 1),
        }

    n = len(rows)
    by_difficulty = {}
    for tag in sorted({row["difficulty"] for row in rows}):
        sub = [row for row in rows if row["difficulty"] == tag]
        by_difficulty[tag] = {
            "n": len(sub),
            "tax_safe_rate": _safe_div(sum(1 for r in sub if r["tax_ok"]), len(sub)),
        }

    errors = [
        {
            "receipt": row["receipt"],
            "bad_fields": {
                f: row["outcomes"][f] for f in TAX_CRITICAL if row["outcomes"][f] in _DANGEROUS
            },
        }
        for row in rows
        if row["tax_danger"]
    ]

    return {
        "summary": {
            "n_receipts": n,
            "n_predicted": sum(1 for r in rows if r["predicted"]),
            "tax_safe_rate": _safe_div(sum(1 for r in rows if r["tax_ok"]), n),
            "tax_danger_count": sum(1 for r in rows if r["tax_danger"]),
        },
        "per_field": per_field,
        "calibration": _calibration(labels, predictions),
        "by_difficulty": by_difficulty,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# IO + runner
# ---------------------------------------------------------------------------


def load_json(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def run_predictions(
    images_dir: Path,
    labels: dict[str, dict],
    *,
    model: str,
    call: Any | None = None,
) -> tuple[dict[str, dict], dict[str, str]]:
    """Run the extractor over every labeled receipt. Returns (predictions, errors).

    `call` is forwarded to `extract_one` so tests can stub the model.
    """
    predictions: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for name in labels:
        image = images_dir / name
        if not image.exists():
            errors[name] = "image file not found"
            continue
        try:
            predictions[name] = asdict(extract_one(image, model=model, call=call))
        except Exception as exc:  # one unreadable photo must not kill the run
            errors[name] = str(exc)
    return predictions, errors


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def render_report_md(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        "# Receipt extraction eval",
        "",
        f"- receipts: **{s['n_receipts']}**  (predicted: {s['n_predicted']})",
        f"- tax-safe receipts — date + state + gallons all correct: **{_pct(s['tax_safe_rate'])}**",
        f"- receipts with a DANGEROUS tax-critical error (wrong/hallucinated): "
        f"**{s['tax_danger_count']}**",
        "",
        "## Per-field outcomes",
        "",
        "| field | correct | wrong | missing | halluc | ok-null | acc (present) | danger |",
        "|---|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for field in SCORED_FIELDS:
        pf = report["per_field"][field]
        c = pf["counts"]
        star = " ⚠️" if field in TAX_CRITICAL else ""
        lines.append(
            f"| {field}{star} | {c['CORRECT']} | {c['WRONG']} | {c['MISSING']} | "
            f"{c['HALLUCINATION']} | {c['CORRECT_NULL']} | "
            f"{_pct(pf['accuracy_when_present'])} | {_pct(pf['danger_rate'])} |"
        )
    lines += [
        "",
        "_⚠️ = tax-critical. WRONG / HALLUC on these are the dangerous ones; MISSING is "
        "safe (a human catches a blank)._",
        "",
        "## Confidence calibration (tax-critical fields)",
        "",
        "Find the lowest confidence bucket whose accuracy clears your bar — that is your "
        "auto-accept threshold. Below it, keep human review.",
        "",
        "| model confidence | graded fields | accuracy |",
        "|---|--:|--:|",
    ]
    for bucket in report["calibration"]:
        lines.append(f"| {bucket['bucket']} | {bucket['n']} | {_pct(bucket['accuracy'])} |")
    lines += ["", "## By difficulty", "", "| difficulty | receipts | tax-safe |", "|---|--:|--:|"]
    for tag, data in report["by_difficulty"].items():
        lines.append(f"| {tag} | {data['n']} | {_pct(data['tax_safe_rate'])} |")
    if report["errors"]:
        lines += ["", "## Dangerous errors to adjudicate", ""]
        for err in report["errors"]:
            bad = ", ".join(f"{k}={v}" for k, v in err["bad_fields"].items())
            lines.append(f"- **{err['receipt']}** — {bad}")
    lines.append("")
    return "\n".join(lines)
