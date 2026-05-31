"""Vision receipt extraction: image discovery, payload coercion, and the
guarantee that what we write is exactly what the existing intake pipeline reads.

The live Claude call is never exercised here — `extract_one` takes an injectable
`call`, so these tests are fully offline and deterministic.
"""

from ifta.intake.extract import (
    candidate_from_payload,
    discover_images,
    extract_one,
    write_candidates_json,
)
from ifta.intake.receipts import review_receipt
from ifta.intake.report import load_receipt_candidates


def test_discover_images_filters_and_sorts(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.heic").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("nope")
    (tmp_path / ".hidden.png").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    assert [p.name for p in discover_images(tmp_path)] == ["a.jpg", "b.heic"]


def test_discover_images_missing_folder(tmp_path):
    assert discover_images(tmp_path / "nope") == []


def test_candidate_from_payload_coerces_and_clamps():
    payload = {
        "date": "2026-04-15",
        "state": "TX",
        "gallons": "112.4 gal",  # stray unit text
        "amount": "$1,421.07",  # currency + thousands separator
        "vendor": "  Loves  ",
        "payment_method": "fleet_card",
        "confidence": {"gallons": 0.91, "state": 1.7, "amount": "high"},
        "totally_unknown_key": "ignored",
    }
    c = candidate_from_payload(payload, source_file="r1.jpg")
    assert c.source_file == "r1.jpg"
    assert c.gallons == 112.4
    assert c.amount == 1421.07
    assert c.state == "TX"
    assert c.vendor == "Loves"  # trimmed
    assert c.confidence["gallons"] == 0.91
    assert c.confidence["state"] == 1.0  # clamped into range
    assert "amount" not in c.confidence  # non-numeric confidence dropped
    assert not hasattr(c, "totally_unknown_key")


def test_unreadable_fields_become_null_and_are_rejected_by_review():
    # A blurry photo: model could read nothing usable.
    c = candidate_from_payload({"date": None, "gallons": None, "confidence": {}}, "blur.jpg")
    assert c.date is None and c.gallons is None and c.state is None
    review = review_receipt(c)
    assert review.status == "REJECTED_MISSING_REQUIRED_DATA"
    assert not review.can_auto_include  # safety layer refuses to use it


def test_bad_payment_method_falls_back_to_unknown():
    c = candidate_from_payload(
        {"gallons": 10, "state": "OH", "date": "2026-01-02", "payment_method": "crypto"},
        "r.jpg",
    )
    assert c.payment_method == "unknown"


def test_extract_one_uses_injected_call(tmp_path):
    img = tmp_path / "receipt.jpg"
    img.write_bytes(b"fake-jpeg-bytes")
    canned = {
        "date": "2026-02-10",
        "state": "CA",
        "gallons": 88.2,
        "amount": 305.5,
        "vendor": "Pilot",
        "payment_method": "fleet_card",
        "confidence": {"date": 0.9, "state": 0.8, "gallons": 0.95},
    }
    c = extract_one(img, model="unused", call=lambda _p: canned)
    assert c.state == "CA"
    assert c.gallons == 88.2
    assert c.source_file == "receipt.jpg"


def test_write_then_load_is_pipeline_compatible(tmp_path):
    """What we write must be exactly what the existing loader/review accepts."""
    candidates = [
        candidate_from_payload(
            {
                "date": "2026-03-01",
                "state": "TX",
                "gallons": 120.0,
                "amount": 450.0,
                "vendor": "TA",
                "payment_method": "fleet_card",
                "confidence": {"gallons": 0.9},
            },
            "a.jpg",
        ),
        candidate_from_payload(
            {
                "date": "2026-03-02",
                "state": "OK",
                "gallons": 60.0,
                "amount": 220.0,
                "vendor": "Love's",
                "card_last4": "1234",
            },
            "b.jpg",
        ),
    ]
    out = tmp_path / "receipt_candidates.json"
    write_candidates_json(candidates, out)

    loaded = load_receipt_candidates(out)
    assert len(loaded) == 2
    assert loaded[0].state == "TX" and loaded[0].gallons == 120.0
    assert loaded[1].card_last4 == "1234"

    # A clean, complete receipt should be usable (pending the normal approval gate).
    review = review_receipt(loaded[0])
    assert review.status.startswith("USABLE")
