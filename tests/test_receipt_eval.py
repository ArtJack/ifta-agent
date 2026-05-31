"""Tests for the receipt eval scorer and metrics — fully offline, no model calls."""

from ifta.receipt_eval import (
    aggregate,
    render_report_md,
    run_predictions,
    score_candidate,
    score_field,
)

# --- the five outcomes -----------------------------------------------------


def test_numeric_field_outcomes():
    assert score_field("gallons", 112.45, 112.4) == "CORRECT"  # within tolerance
    assert score_field("gallons", 100, 120) == "WRONG"
    assert score_field("gallons", None, 120) == "MISSING"
    assert score_field("gallons", 50, None) == "HALLUCINATION"
    assert score_field("gallons", None, None) == "CORRECT_NULL"


def test_blank_strings_count_as_absent():
    assert score_field("state", "", "TX") == "MISSING"
    assert score_field("state", "N/A", None) == "CORRECT_NULL"


def test_state_is_case_insensitive():
    assert score_field("state", "tx", "TX") == "CORRECT"
    assert score_field("state", "TX", "OK") == "WRONG"


def test_date_normalizes_formats():
    assert score_field("date", "03/01/2026", "2026-03-01") == "CORRECT"
    assert score_field("date", "2026-03-02", "2026-03-01") == "WRONG"


def test_card_last4_compares_digits_only():
    assert score_field("card_last4", "xxxx-1234", "1234") == "CORRECT"


def test_vendor_is_lenient():
    assert score_field("vendor", "Pilot", "Pilot Travel Center") == "CORRECT"
    assert score_field("vendor", "Pilot", "Love's") == "WRONG"


def test_score_candidate_covers_all_scored_fields():
    outcomes = score_candidate({"state": "TX"}, {"state": "TX", "gallons": 10})
    assert outcomes["state"] == "CORRECT"
    assert outcomes["gallons"] == "MISSING"
    assert "payment_method" in outcomes


# --- aggregate -------------------------------------------------------------


def _fixture():
    labels = {
        "r1.jpg": {"date": "2026-03-01", "state": "TX", "gallons": 120, "_difficulty": "clean"},
        "r2.jpg": {"date": "2026-03-02", "state": "OK", "gallons": 60, "_difficulty": "faded"},
    }
    predictions = {
        # r1: perfect, high confidence
        "r1.jpg": {
            "date": "2026-03-01", "state": "TX", "gallons": 120.0,
            "confidence": {"date": 0.95, "state": 0.93, "gallons": 0.97},
        },
        # r2: state hallucinated wrong with mid confidence -> dangerous
        "r2.jpg": {
            "date": "2026-03-02", "state": "CA", "gallons": 60.0,
            "confidence": {"date": 0.9, "state": 0.7, "gallons": 0.9},
        },
    }
    return labels, predictions


def test_aggregate_tax_safety_and_errors():
    labels, predictions = _fixture()
    report = aggregate(labels, predictions)

    assert report["summary"]["n_receipts"] == 2
    assert report["summary"]["tax_safe_rate"] == 0.5  # only r1 is fully correct
    assert report["summary"]["tax_danger_count"] == 1  # r2 has a wrong state

    assert report["per_field"]["state"]["counts"]["CORRECT"] == 1
    assert report["per_field"]["state"]["counts"]["WRONG"] == 1

    assert len(report["errors"]) == 1
    assert report["errors"][0]["receipt"] == "r2.jpg"
    assert report["errors"][0]["bad_fields"] == {"state": "WRONG"}


def test_calibration_buckets_separate_good_and_bad_confidence():
    labels, predictions = _fixture()
    report = aggregate(labels, predictions)
    buckets = {b["bucket"]: b for b in report["calibration"]}

    # r1's three correct tax fields + r2's correct date/gallons all sit at >=0.9.
    assert buckets["0.90-1.00"]["accuracy"] == 1.0
    # r2's wrong state (conf 0.7) is the only thing in the 0.5-0.8 bucket -> 0% accurate.
    assert buckets["0.50-0.80"]["n"] == 1
    assert buckets["0.50-0.80"]["accuracy"] == 0.0


def test_missing_prediction_scores_as_missing_not_wrong():
    labels = {"r1.jpg": {"state": "TX", "gallons": 10, "date": "2026-01-01"}}
    report = aggregate(labels, {})  # model returned nothing
    assert report["summary"]["tax_danger_count"] == 0  # MISSING is not dangerous
    assert report["per_field"]["state"]["counts"]["MISSING"] == 1


def test_render_report_md_is_stringy():
    labels, predictions = _fixture()
    md = render_report_md(aggregate(labels, predictions))
    assert "# Receipt extraction eval" in md
    assert "tax-safe" in md
    assert "Confidence calibration" in md


def test_run_predictions_uses_injected_call(tmp_path):
    (tmp_path / "r1.jpg").write_bytes(b"fake")
    labels = {"r1.jpg": {"state": "TX"}, "missing.jpg": {"state": "OH"}}
    canned = {"state": "TX", "gallons": 99.0, "confidence": {"state": 0.9}}
    preds, errors = run_predictions(tmp_path, labels, model="x", call=lambda _p: canned)
    assert preds["r1.jpg"]["state"] == "TX"
    assert "missing.jpg" in errors  # image file absent
