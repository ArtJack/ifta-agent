"""Tests for the benchmark gate — fully offline."""

from ifta.benchmark import (
    DEFAULT_THRESHOLDS,
    append_history,
    compare,
    evaluate,
    load_history,
    scorecard,
)

_ALL_FIELDS = (
    "date", "state", "gallons", "amount", "vendor",
    "fuel_type", "truck_id", "card_last4", "invoice", "payment_method",
)


def _report(*, tax_safe=1.0, danger=0, fields=None):
    acc = dict.fromkeys(_ALL_FIELDS, 1.0)
    acc.update(fields or {})
    return {
        "summary": {"n_receipts": 47, "n_predicted": 47,
                    "tax_safe_rate": tax_safe, "tax_danger_count": danger},
        "per_field": {f: {"accuracy_when_present": a} for f, a in acc.items()},
    }


def test_clean_run_passes_the_gate():
    result = evaluate(_report())
    assert result.passed
    assert all(c.passed for c in result.checks)


def test_a_single_dangerous_error_fails():
    result = evaluate(_report(danger=1))
    assert not result.passed
    failed = [c.name for c in result.checks if not c.passed]
    assert "dangerous_tax_errors" in failed


def test_low_tax_critical_field_fails():
    result = evaluate(_report(fields={"gallons": 0.80}))
    assert not result.passed
    assert "gallons_accuracy" in [c.name for c in result.checks if not c.passed]


def test_missing_field_data_fails_safe():
    result = evaluate(_report(fields={"state": None}))
    assert not result.passed  # can't verify -> don't pass


def test_low_tax_safe_rate_fails():
    assert not evaluate(_report(tax_safe=0.90)).passed


def test_non_tax_field_does_not_break_the_gate():
    # card_last4 is not in the threshold set, so a dip there alone still passes.
    assert evaluate(_report(fields={"card_last4": 0.50})).passed


# --- regression detection ---------------------------------------------------


def test_compare_flags_a_real_regression():
    base = scorecard(_report(fields={"card_last4": 0.91}), model="m")
    cur = scorecard(_report(fields={"card_last4": 0.62}), model="m")
    cmp = compare(cur, base, max_regression=DEFAULT_THRESHOLDS["max_regression"])
    assert any(r["field"] == "card_last4" for r in cmp["regressions"])


def test_compare_ignores_small_wobble():
    base = scorecard(_report(fields={"vendor": 0.80}), model="m")
    cur = scorecard(_report(fields={"vendor": 0.79}), model="m")  # -1pt, within tolerance
    assert compare(cur, base)["regressions"] == []


# --- history ----------------------------------------------------------------


def test_scorecard_is_compact_and_pii_free():
    card = scorecard(_report(), model="claude-sonnet-4-6", note="baseline")
    assert card["model"] == "claude-sonnet-4-6"
    assert card["tax_safe_rate"] == 1.0
    assert set(card["fields"]) == set(_ALL_FIELDS)
    assert "receipt" not in card and "ts" in card  # no per-receipt content


def test_history_append_and_load_roundtrip(tmp_path):
    path = tmp_path / "history.jsonl"
    append_history(scorecard(_report(), model="a"), path)
    append_history(scorecard(_report(tax_safe=0.97), model="b"), path)
    history = load_history(path)
    assert len(history) == 2
    assert [h["model"] for h in history] == ["a", "b"]
    assert load_history(tmp_path / "nope.jsonl") == []
