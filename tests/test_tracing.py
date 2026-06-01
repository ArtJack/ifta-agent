"""Tests for review-agent tracing — offline, with fake SDK messages."""

import json
from types import SimpleNamespace

from ifta.agent.tracing import (
    Trace,
    current_trace,
    render_trace,
    save_trace,
    traced,
)


def _block(**kw):
    return SimpleNamespace(**kw)


def _msg(blocks, in_tok=0, out_tok=0):
    return SimpleNamespace(
        content=blocks,
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
    )


def test_record_turn_extracts_text_thinking_and_tools():
    trace = Trace(label="x", model="m")
    trace.record_turn(
        _msg(
            [
                _block(type="thinking", thinking="…"),
                _block(type="tool_use", name="lookup_rate", input={"state": "KY"}),
                _block(type="text", text="Here is the review."),
            ],
            in_tok=100,
            out_tok=20,
        )
    )
    turn = trace.turns[0]
    assert turn.thinking is True
    assert turn.tool_calls[0].name == "lookup_rate"
    assert turn.tool_calls[0].input == {"state": "KY"}
    assert "review" in turn.text
    assert turn.input_tokens == 100 and turn.output_tokens == 20


def test_tool_sequence_orders_across_turns():
    trace = Trace(label="x", model="m")
    trace.record_turn(_msg([_block(type="tool_use", name="a", input={})]))
    trace.record_turn(
        _msg([_block(type="tool_use", name="b", input={}), _block(type="tool_use", name="c", input={})])
    )
    assert trace.tool_sequence() == ["a", "b", "c"]


def test_close_aggregates_counts_and_metrics():
    trace = Trace(label="x", model="m")
    trace.record_turn(_msg([_block(type="tool_use", name="a", input={})]))
    trace.record_turn(_msg([_block(type="text", text="done")]))
    trace.close(
        final_text="final",
        metrics=SimpleNamespace(wall_time_seconds=1.5, estimated_cost_usd=0.02),
        filing_status="READY_TO_FILE",
    )
    assert trace.n_model_calls == 2
    assert trace.n_tool_calls == 1
    assert trace.wall_time_seconds == 1.5
    assert trace.estimated_cost_usd == 0.02
    assert trace.filing_status == "READY_TO_FILE"


def test_traced_sets_and_resets_the_contextvar():
    assert current_trace() is None
    with traced("lbl", "m") as tr:
        assert current_trace() is tr
    assert current_trace() is None  # reset even after the block


def test_render_and_save_roundtrip(tmp_path):
    trace = Trace(label="review Q1-2026", model="claude-sonnet-4-6")
    trace.record_turn(_msg([_block(type="tool_use", name="query_return", input={"q": "Q1-2026"})], 50, 10))
    trace.close(final_text="All good", filing_status="READY_TO_FILE")

    out = render_trace(trace)
    assert "query_return" in out and "review Q1-2026" in out and "READY_TO_FILE" in out

    path = save_trace(trace, tmp_path / "tr.json")
    data = json.loads(path.read_text())
    assert data["label"] == "review Q1-2026"
    assert data["turns"][0]["tool_calls"][0]["name"] == "query_return"


def test_run_agent_records_into_the_active_trace(monkeypatch):
    """The run_agent loop populates a trace when one is active, and is a no-op otherwise."""
    from ifta.agent import runner
    from ifta.agent.tracing import traced

    messages = [
        _msg([_block(type="tool_use", name="query_return", input={"quarter": "Q1-2026"})], 30, 8),
        _msg([_block(type="text", text="Looks good.")], 12, 6),
    ]

    class _Messages:
        @staticmethod
        def tool_runner(**_kwargs):
            return iter(messages)

    class _Beta:
        messages = _Messages()

    class _FakeClient:
        beta = _Beta()

    monkeypatch.setattr(runner, "_client", lambda: _FakeClient())

    # no active trace -> runs fine, captures nothing
    text, _, _ = runner.run_agent("hi", model="claude-haiku-4-5")
    assert "Looks good" in text

    # active trace -> turns + tool sequence captured
    with traced("review Q1-2026", "claude-haiku-4-5") as trace:
        runner.run_agent("hi", model="claude-haiku-4-5")
    assert len(trace.turns) == 2
    assert trace.tool_sequence() == ["query_return"]
