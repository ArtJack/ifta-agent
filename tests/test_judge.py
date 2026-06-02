"""LLM rubric judge + judge-validation (agreement) — fully offline.

The live model call is bypassed everywhere via the injected `call` hook, so these
tests pin the parsing, prompt-building, scoring math, and agreement mechanism
without touching the network.
"""

from ifta.eval.judge import (
    RUBRIC,
    JudgeResult,
    agreement,
    build_prompt,
    judge_review,
    parse_judge,
    render_judge,
)


def test_parse_judge_clamps_rounds_and_drops_non_numeric():
    r = parse_judge(
        {
            "coverage": 5,  # clamps down to 2
            "grounding": -3,  # clamps up to 0
            "clarity": 1.6,  # rounds to 2
            "filing_alignment": True,  # bool dropped, not treated as 1
            "rationale": "not-a-dict",  # ignored
        }
    )
    assert r.scores["coverage"] == 2
    assert r.scores["grounding"] == 0
    assert r.scores["clarity"] == 2
    assert "filing_alignment" not in r.scores
    assert r.rationale == {}


def test_judge_result_overall_is_normalized_to_0_1():
    r = JudgeResult(scores={"coverage": 2, "grounding": 1, "clarity": 2, "filing_alignment": 2}, rationale={})
    assert r.overall == (2 + 1 + 2 + 2) / 8
    assert JudgeResult(scores={}, rationale={}).overall == 0.0


def test_build_prompt_embeds_status_reasons_rubric_and_survives_braces():
    # A review note containing literal { } must not break str.format().
    prompt = build_prompt(
        "Net due {KY}: $5.00",
        filing_status="DO_NOT_FILE",
        filing_reasons=["missing 2 receipts", "rate stale"],
    )
    assert "DO_NOT_FILE" in prompt
    assert "missing 2 receipts" in prompt
    assert "rate stale" in prompt
    assert "Net due {KY}: $5.00" in prompt
    for c in RUBRIC:
        assert c.name in prompt


def test_judge_review_uses_injected_call_and_parses():
    captured = {}

    def fake(prompt):
        captured["prompt"] = prompt
        return {
            "coverage": 2,
            "grounding": 1,
            "clarity": 2,
            "filing_alignment": 2,
            "rationale": {"coverage": "addresses both blockers"},
        }

    result = judge_review(
        "some note",
        filing_status="DO_NOT_FILE",
        filing_reasons=["missing receipts"],
        call=fake,
    )
    assert result.scores == {"coverage": 2, "grounding": 1, "clarity": 2, "filing_alignment": 2}
    assert result.rationale["coverage"] == "addresses both blockers"
    assert result.overall == (2 + 1 + 2 + 2) / 8
    # the prompt the judge actually saw carried the deterministic context
    assert "DO_NOT_FILE" in captured["prompt"]
    assert "missing receipts" in captured["prompt"]


def test_agreement_reports_per_criterion_and_rates():
    ag = agreement(
        {"coverage": 2, "grounding": 1, "clarity": 0},
        {"coverage": 2, "grounding": 2, "clarity": 2},
    )
    assert ag["per_criterion"]["coverage"]["delta"] == 0
    assert ag["per_criterion"]["grounding"]["delta"] == 1
    assert ag["per_criterion"]["clarity"]["delta"] == 2
    assert ag["exact_rate"] == 1 / 3  # only coverage matches exactly
    assert ag["within1_rate"] == 2 / 3  # coverage + grounding within 1


def test_agreement_ignores_criteria_missing_from_either_side():
    ag = agreement({"coverage": 2}, {"coverage": 2, "grounding": 0})
    assert set(ag["per_criterion"]) == {"coverage"}  # grounding only on one side
    assert ag["exact_rate"] == 1.0


def test_agreement_with_no_overlap_yields_none_rates():
    ag = agreement({}, {})
    assert ag["per_criterion"] == {}
    assert ag["exact_rate"] is None
    assert ag["within1_rate"] is None


def test_render_judge_shows_scores_overall_and_missing_dash():
    out = render_judge(
        JudgeResult(scores={"coverage": 2, "grounding": 1}, rationale={"coverage": "good"})
    )
    assert "coverage" in out
    assert "2/2" in out
    assert "overall" in out
    assert "75%" in out  # (2+1)/(2*2)
    assert "—" in out  # clarity + filing_alignment unscored
