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
            "filing_status": "READY_WITH_WARNINGS",
            "filing_status_reasons": ["One warning remains."],
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
    assert note.filing_status == "READY_WITH_WARNINGS"
    assert note.filing_status_reasons == ["One warning remains."]
    assert format_review_item(note.issues[0]) == "[warning] rate_fallback: Rates fell back."


def _capture_web_context(monkeypatch):
    """Stub the model call and capture the client-context dict review() builds."""
    from ifta.agent import runner

    captured: dict = {}

    def fake_retry(*, quarter, client_context_dict, model, max_tokens, effort):
        captured.update(client_context_dict)
        return ReviewNote(summary="ok", issues=[], filing_reminders=[], next_steps=[]), object()

    monkeypatch.setattr(runner, "_run_review_with_retry", fake_retry)
    return runner, captured


def test_web_review_threads_base_state_into_context(monkeypatch, tmp_path) -> None:
    """The intake-form base state must reach the agent (uppercased), so it
    never flags a base jurisdiction the customer already provided."""
    runner, captured = _capture_web_context(monkeypatch)
    inbox, out = tmp_path / "in", tmp_path / "out"
    inbox.mkdir()
    out.mkdir()
    runner.review(
        "Q1-2026", inbox_dir=inbox, output_dir=out, client_name="DM Express", base_state="ca"
    )
    assert captured["base_jurisdiction"] == "CA"
    assert "did not provide" not in captured["notes"]
    assert "CA" in captured["notes"]


def test_web_review_without_base_state_notes_absence(monkeypatch, tmp_path) -> None:
    runner, captured = _capture_web_context(monkeypatch)
    inbox, out = tmp_path / "in", tmp_path / "out"
    inbox.mkdir()
    out.mkdir()
    runner.review("Q1-2026", inbox_dir=inbox, output_dir=out, client_name="X", base_state=None)
    assert captured["base_jurisdiction"] is None
    assert "did not provide" in captured["notes"]
