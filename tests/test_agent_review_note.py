"""Agent review-note formatting guards."""

from ifta.agent import ReviewNote, format_review_item, review_note_from_payload, write_review_md


def test_review_note_writer_renders_structured_items(tmp_path) -> None:
    note = ReviewNote(
        summary="Return reviewed for TEST LOGISTICS LLC.",
        issues=[
            {
                "id": "truck_count_mismatch",
                "severity": "high",
                "detail": "Return shows 5 trucks; expected source should be confirmed.",
            }
        ],
        filing_reminders=[
            {
                "item": "deadline",
                "detail": "Q2-2026 IFTA return is due July 31, 2026.",
            }
        ],
        next_steps=[
            {
                "todo": "reconcile_trucks",
                "detail": "Confirm the Test Logistics fleet list before filing.",
            }
        ],
    )

    path = write_review_md(note, tmp_path / "review_note.md")
    text = path.read_text(encoding="utf-8")

    assert "{'id'" not in text
    assert '"id"' not in text
    assert "- [high] truck_count_mismatch: Return shows 5 trucks" in text
    assert "- deadline: Q2-2026 IFTA return is due July 31, 2026." in text
    assert "- [ ] reconcile_trucks: Confirm the Test Logistics fleet list" in text


def test_review_payload_normalizer_accepts_strings_dicts_and_single_items() -> None:
    note = review_note_from_payload(
        {
            "summary": "Looks reviewable.",
            "issues": {"id": "rate_fallback", "severity": "warning", "detail": "Rates fell back."},
            "filing_reminders": ["Keep records for 4 years."],
            "next_steps": "Verify current-quarter rates.",
        }
    )

    assert note.summary == "Looks reviewable."
    assert note.issues == [
        {"id": "rate_fallback", "severity": "warning", "detail": "Rates fell back."}
    ]
    assert note.filing_reminders == ["Keep records for 4 years."]
    assert note.next_steps == ["Verify current-quarter rates."]
    assert format_review_item(note.issues[0]) == "[warning] rate_fallback: Rates fell back."
