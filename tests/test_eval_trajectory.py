"""Trajectory (span) grading for the agent eval — offline.

`grade_trajectory` is pure; `run_case` is exercised with a faked agent that records
a tool call through the active trace, proving the trace -> trajectory-grading wiring.
"""

from types import SimpleNamespace

from ifta.eval import EvalCase, grade_trajectory, run_case


def _by_name(results):
    return {r.name: r.passed for r in results}


def test_must_call_and_must_not_call():
    seq = ["query_return", "lookup_rate", "query_findings"]
    res = _by_name(
        grade_trajectory(
            {"must_call": ["query_return", "get_regulations"], "must_not_call": ["read_past_filing", "lookup_rate"]},
            seq,
        )
    )
    assert res["must_call[query_return]"] is True
    assert res["must_call[get_regulations]"] is False  # never called
    assert res["must_not_call[read_past_filing]"] is True
    assert res["must_not_call[lookup_rate]"] is False  # forbidden tool was called


def test_call_count_bounds():
    res = _by_name(grade_trajectory({"max_calls": 2, "min_calls": 4}, ["a", "b", "c"]))
    assert res["max_calls<=2"] is False  # 3 > 2
    assert res["min_calls>=4"] is False  # 3 < 4


def test_must_call_in_order_is_a_subsequence_check():
    ok = grade_trajectory({"must_call_in_order": ["a", "c"]}, ["a", "b", "c"])
    bad = grade_trajectory({"must_call_in_order": ["c", "a"]}, ["a", "b", "c"])
    assert _by_name(ok)["must_call_in_order['a', 'c']"] is True
    assert _by_name(bad)["must_call_in_order['c', 'a']"] is False


def test_empty_spec_yields_no_checks():
    assert grade_trajectory({}, ["a", "b"]) == []


def test_run_case_grades_trajectory_from_the_trace(monkeypatch):
    from ifta.agent.runner import ReviewNote
    from ifta.agent.tracing import current_trace
    from ifta.eval import runner as eval_runner

    def fake_review(_quarter, **_kw):
        active = current_trace()
        if active is not None:  # simulate the agent calling a tool mid-run
            active.record_turn(
                SimpleNamespace(
                    content=[SimpleNamespace(type="tool_use", name="query_return", input={})],
                    usage=None,
                )
            )
        return ReviewNote(summary="Net tax due $0.00", issues=["x"], filing_reminders=[], next_steps=[]), None

    monkeypatch.setattr(eval_runner, "review", fake_review)
    case = EvalCase(
        name="t",
        description="",
        command="review",
        quarter="Q4-2025",
        assertions={"tools": {"must_call": ["query_return"], "must_not_call": ["read_past_filing"]}},
    )
    result = run_case(case)
    assert result.tool_sequence == ["query_return"]
    names = _by_name(result.assertions)
    assert names["must_call[query_return]"] is True
    assert names["must_not_call[read_past_filing]"] is True
