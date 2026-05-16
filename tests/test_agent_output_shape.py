"""Pure (no-API) tests for agent output normalization.

These guard against the regression where the agent returned a list of dicts
instead of a list of strings and our writer printed raw Python dict syntax
into the deliverable Markdown.
"""

from pathlib import Path

from ifta.agent import (
    ReviewNote,
    format_review_item,
    review_note_from_payload,
    write_review_md,
)

# ---------------------------------------------------------------------------
# review_note_from_payload — accepts strings, dicts, mixed, missing
# ---------------------------------------------------------------------------


def test_payload_with_plain_string_items() -> None:
    note = review_note_from_payload(
        {
            "summary": "Looks clean.",
            "issues": ["KY surcharge missing"],
            "filing_reminders": ["Due July 31"],
            "next_steps": ["Upload to portal"],
        }
    )
    assert note.summary == "Looks clean."
    assert note.issues == ["KY surcharge missing"]
    assert note.filing_reminders == ["Due July 31"]
    assert note.next_steps == ["Upload to portal"]


def test_payload_with_dict_items_preserves_them() -> None:
    note = review_note_from_payload(
        {
            "summary": "OK.",
            "issues": [
                {"id": "ky_surcharge_missing", "severity": "high", "detail": "Add KY line."}
            ],
            "filing_reminders": [],
            "next_steps": [{"todo": "Submit", "action": "Click upload"}],
        }
    )
    assert isinstance(note.issues[0], dict)
    assert note.issues[0]["id"] == "ky_surcharge_missing"
    assert isinstance(note.next_steps[0], dict)


def test_payload_with_mixed_items() -> None:
    note = review_note_from_payload(
        {
            "summary": "Mixed.",
            "issues": [
                "plain string issue",
                {"id": "x", "severity": "low", "detail": "second"},
                None,  # gets dropped
                "  ",  # whitespace-only string gets dropped
            ],
            "filing_reminders": None,
            "next_steps": None,
        }
    )
    assert len(note.issues) == 2
    assert note.issues[0] == "plain string issue"
    assert isinstance(note.issues[1], dict)
    assert note.filing_reminders == []
    assert note.next_steps == []


def test_payload_with_missing_fields() -> None:
    note = review_note_from_payload({"summary": "Only summary."})
    assert note.summary == "Only summary."
    assert note.issues == []
    assert note.filing_reminders == []
    assert note.next_steps == []


def test_payload_with_single_non_list_item_gets_wrapped() -> None:
    note = review_note_from_payload(
        {
            "summary": "x",
            "issues": {"id": "a", "detail": "b"},  # not a list — should be wrapped
            "filing_reminders": "one reminder string",
            "next_steps": [],
        }
    )
    assert len(note.issues) == 1 and isinstance(note.issues[0], dict)
    assert note.filing_reminders == ["one reminder string"]


# ---------------------------------------------------------------------------
# format_review_item — renders cleanly, no raw dict syntax leaks
# ---------------------------------------------------------------------------


def test_format_plain_string() -> None:
    assert format_review_item("Hello world") == "Hello world"


def test_format_string_with_checkbox() -> None:
    assert format_review_item("Do thing", checkbox=True) == "[ ] Do thing"


def test_format_dict_with_id_and_detail() -> None:
    out = format_review_item({"id": "ky_surcharge", "detail": "Add the line."})
    assert out == "ky_surcharge: Add the line."
    assert "{" not in out  # no Python dict repr leaking


def test_format_dict_with_severity_prefix() -> None:
    out = format_review_item(
        {"id": "mpg_low", "severity": "high", "detail": "Fleet MPG below floor."}
    )
    assert out.startswith("[high]")
    assert "mpg_low: Fleet MPG below floor." in out


def test_format_dict_detail_only() -> None:
    out = format_review_item({"detail": "Just a note."})
    assert out == "Just a note."


def test_format_dict_todo_with_action() -> None:
    out = format_review_item(
        {"todo": "submit_return", "action": "Click upload in CDTFA."}, checkbox=True
    )
    assert out.startswith("[ ]")
    assert "submit_return" in out and "Click upload in CDTFA." in out


def test_format_dict_unknown_keys_falls_back_to_json() -> None:
    # Unrecognised shape — must NOT leak Python repr, must serialise as JSON.
    out = format_review_item({"random": "thing", "weight": 12})
    assert "'random'" not in out  # not Python repr
    assert '"random": "thing"' in out  # actual JSON


# ---------------------------------------------------------------------------
# write_review_md — produces clean Markdown for any input
# ---------------------------------------------------------------------------


def test_write_review_md_clean_dict_items(tmp_path: Path) -> None:
    note = ReviewNote(
        summary="Q2 looks ready.",
        issues=[
            {
                "id": "ky_surcharge_present",
                "severity": "info",
                "detail": "KY surcharge $73.08 included.",
            }
        ],
        filing_reminders=[
            "Due July 31, 2026",
            {"item": "or_wmt", "detail": "Oregon WMT filed separately."},
        ],
        next_steps=[{"todo": "verify_receipts", "action": "Pull NC fuel receipts."}],
    )
    out_path = tmp_path / "review.md"
    write_review_md(note, out_path)
    text = out_path.read_text()

    # NEVER allow raw Python dict syntax to leak into the deliverable
    assert "{'id'" not in text
    assert "{'item'" not in text
    assert "{'todo'" not in text

    # Verify expected content is present
    assert "## Summary" in text
    assert "Q2 looks ready." in text
    assert "[info]" in text
    assert "ky_surcharge_present" in text
    assert "Due July 31, 2026" in text
    assert "Oregon WMT filed separately." in text
    # Next steps render as checkboxes
    assert "- [ ]" in text
    assert "verify_receipts" in text


def test_write_review_md_omits_empty_sections(tmp_path: Path) -> None:
    note = ReviewNote(summary="No issues.", issues=[], filing_reminders=[], next_steps=[])
    out_path = tmp_path / "minimal.md"
    write_review_md(note, out_path)
    text = out_path.read_text()
    assert "## Summary" in text
    assert "## Issues" not in text
    assert "## Filing reminders" not in text
    assert "## Next steps" not in text
