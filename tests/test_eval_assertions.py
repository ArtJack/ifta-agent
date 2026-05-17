"""Unit tests for the eval-grading logic — no LLM calls.

Exercises grade_assertions() against fake response_text + ReviewNote pairs
so we know the grader itself behaves correctly. The real `ifta eval`
suite hits the model.
"""

from __future__ import annotations

import json
from pathlib import Path

from ifta.agent.runner import ReviewNote
from ifta.eval import EvalCase, grade_assertions, load_cases


def _note(summary: str = "", issues=None, reminders=None, steps=None) -> ReviewNote:
    return ReviewNote(
        summary=summary,
        issues=issues or [],
        filing_reminders=reminders or [],
        next_steps=steps or [],
    )


def _passed(results) -> list[str]:
    return [r.name for r in results if r.passed]


def _failed(results) -> list[str]:
    return [r.name for r in results if not r.passed]


# ---------------------------------------------------------------------------
# must_mention / must_not_mention
# ---------------------------------------------------------------------------


def test_must_mention_pass() -> None:
    res = grade_assertions(
        {"must_mention": ["Kentucky"]},
        response_text="filing was submitted to Kentucky DOR",
        note=None,
    )
    assert len(res) == 1
    assert res[0].passed


def test_must_mention_case_insensitive() -> None:
    res = grade_assertions(
        {"must_mention": ["KENTUCKY"]},
        response_text="filed to kentucky",
        note=None,
    )
    assert res[0].passed


def test_must_mention_fail() -> None:
    res = grade_assertions(
        {"must_mention": ["California"]},
        response_text="filing in Texas",
        note=None,
    )
    assert not res[0].passed
    assert "missing" in res[0].detail


def test_must_not_mention_pass() -> None:
    res = grade_assertions(
        {"must_not_mention": ["MENSHIKOV"]},
        response_text="DM EXPRESS quarterly review",
        note=None,
    )
    assert res[0].passed


def test_must_not_mention_fail_leak() -> None:
    res = grade_assertions(
        {"must_not_mention": ["MENSHIKOV"]},
        response_text="this looks similar to MENSHIKOV's pattern",
        note=None,
    )
    assert not res[0].passed


# ---------------------------------------------------------------------------
# total_tax_due
# ---------------------------------------------------------------------------


def test_total_tax_due_pass_exact_in_summary() -> None:
    note = _note(summary="Net tax due $795.16 across 34 jurisdictions.")
    res = grade_assertions(
        {"total_tax_due": 795.16},
        response_text=note.summary,
        note=note,
    )
    assert res[0].passed


def test_total_tax_due_pass_with_comma() -> None:
    note = _note(summary="Total tax $3,216.33 due.")
    res = grade_assertions(
        {"total_tax_due": 3216.33},
        response_text=note.summary,
        note=note,
    )
    assert res[0].passed


def test_total_tax_due_fail_wrong_number() -> None:
    note = _note(summary="Net tax due $800.00.")
    res = grade_assertions(
        {"total_tax_due": 795.16},
        response_text=note.summary,
        note=note,
    )
    assert not res[0].passed


# ---------------------------------------------------------------------------
# structural / length checks
# ---------------------------------------------------------------------------


def test_min_summary_len() -> None:
    short = _note(summary="ok")
    long = _note(summary="x" * 200)
    res_short = grade_assertions(
        {"min_summary_len": 100}, response_text=short.summary, note=short
    )
    res_long = grade_assertions(
        {"min_summary_len": 100}, response_text=long.summary, note=long
    )
    assert not res_short[0].passed
    assert res_long[0].passed


def test_min_issues() -> None:
    note = _note(summary="ok", issues=["one"])
    res = grade_assertions({"min_issues": 2}, response_text="", note=note)
    assert not res[0].passed
    res2 = grade_assertions(
        {"min_issues": 1},
        response_text="",
        note=_note(summary="ok", issues=["one"]),
    )
    assert res2[0].passed


def test_structural_sections() -> None:
    note = _note(
        summary="hi",
        issues=["a"],
        reminders=["b"],
        steps=["c"],
    )
    res = grade_assertions(
        {
            "structural": {
                "has_summary": True,
                "has_issues": True,
                "has_filing_reminders": True,
                "has_next_steps": True,
            }
        },
        response_text="",
        note=note,
    )
    assert all(r.passed for r in res), [r.name for r in res if not r.passed]


def test_structural_missing_section_fails() -> None:
    note = _note(summary="hi")  # no issues/reminders/steps
    res = grade_assertions(
        {
            "structural": {
                "has_summary": True,
                "has_issues": True,
            }
        },
        response_text="",
        note=note,
    )
    assert _passed(res) == ["structural.has_summary=True"]
    assert _failed(res) == ["structural.has_issues=True"]


# ---------------------------------------------------------------------------
# Case loader
# ---------------------------------------------------------------------------


def test_load_cases_reads_starter_set() -> None:
    cases = load_cases()
    assert len(cases) >= 5, [c.name for c in cases]
    names = {c.name for c in cases}
    assert "q4_2025_menshikov_baseline" in names
    assert "isolation_david_with_menshikov_question" in names
    assert "injection_ignore_instructions" in names


def test_load_cases_ignores_underscore_prefix(tmp_path: Path) -> None:
    (tmp_path / "real.json").write_text(
        json.dumps(
            {
                "name": "real",
                "description": "",
                "command": "review",
                "quarter": "Q4-2025",
            }
        )
    )
    (tmp_path / "_draft.json").write_text(
        json.dumps(
            {
                "name": "draft",
                "description": "",
                "command": "review",
                "quarter": "Q4-2025",
            }
        )
    )
    cases = load_cases(tmp_path)
    assert [c.name for c in cases] == ["real"]


def test_eval_case_from_json_roundtrip(tmp_path: Path) -> None:
    src = {
        "name": "demo",
        "description": "demo case",
        "command": "review",
        "quarter": "Q4-2025",
        "client": "menshikov_llc",
        "max_tokens": 1024,
        "assertions": {"must_mention": ["KY"]},
    }
    path = tmp_path / "demo.json"
    path.write_text(json.dumps(src))
    case = EvalCase.from_json(path)
    assert case.name == "demo"
    assert case.client == "menshikov_llc"
    assert case.assertions == {"must_mention": ["KY"]}
    assert case.max_tokens == 1024
