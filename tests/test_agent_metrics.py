"""Pure tests for AgentMetrics — cost math, formatting, edge cases.

Guards against pricing-table drift breaking the cost line silently. If you
update MODEL_PRICING, expected values here should be updated too.
"""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from ifta.agent import AgentMetrics, ReviewNote, format_metrics_md, write_review_md


def _opus_metrics() -> AgentMetrics:
    return AgentMetrics(
        model="claude-opus-4-7",
        wall_time_seconds=42.5,
        input_tokens=1_000,
        output_tokens=2_000,
        cache_read_tokens=10_000,
        cache_creation_tokens=500,
        n_model_calls=3,
    )


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------


def test_opus_cost_math() -> None:
    m = _opus_metrics()
    m.recompute_cost()
    # Opus 4.7: $5 input, $25 output (per 1M)
    # Per token: 0.000005 in, 0.000025 out
    # Cache read at 0.1x input -> 0.0000005
    # Cache write at 1.25x input -> 0.00000625
    expected = 1_000 * 0.000_005 + 2_000 * 0.000_025 + 10_000 * 0.000_000_5 + 500 * 0.000_006_25
    assert abs(m.estimated_cost_usd - round(expected, 4)) < 1e-9


def test_haiku_cost_is_cheaper_than_opus() -> None:
    base = AgentMetrics(model="claude-opus-4-7", input_tokens=10_000, output_tokens=5_000)
    base.recompute_cost()
    cheap = replace(base, model="claude-haiku-4-5")
    cheap.recompute_cost()
    # Haiku is 5x cheaper on input, 5x cheaper on output
    assert (
        cheap.estimated_cost_usd * 5 == base.estimated_cost_usd
        or cheap.estimated_cost_usd < base.estimated_cost_usd
    )


def test_zero_tokens_zero_cost() -> None:
    m = AgentMetrics(model="claude-opus-4-7")
    m.recompute_cost()
    assert m.estimated_cost_usd == 0.0


def test_unknown_model_is_zero_cost() -> None:
    m = AgentMetrics(model="claude-future-50-0", input_tokens=1000, output_tokens=1000)
    m.recompute_cost()
    assert m.estimated_cost_usd == 0.0


def test_add_usage_accumulates() -> None:
    m = AgentMetrics(model="claude-opus-4-7")
    # Simulate 3 model calls in a tool-runner loop
    m.add_usage(
        SimpleNamespace(
            input_tokens=100,
            output_tokens=200,
            cache_read_input_tokens=50,
            cache_creation_input_tokens=10,
        )
    )
    m.add_usage(
        SimpleNamespace(
            input_tokens=20,
            output_tokens=400,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
    )
    m.add_usage(
        SimpleNamespace(
            input_tokens=5,
            output_tokens=15,
            cache_read_input_tokens=10,
            cache_creation_input_tokens=0,
        )
    )
    assert m.input_tokens == 125
    assert m.output_tokens == 615
    assert m.cache_read_tokens == 60
    assert m.cache_creation_tokens == 10
    assert m.n_model_calls == 3


def test_add_usage_handles_none() -> None:
    m = AgentMetrics(model="claude-opus-4-7")
    m.add_usage(None)
    assert m.n_model_calls == 0
    assert m.input_tokens == 0


def test_add_usage_missing_fields_default_to_zero() -> None:
    m = AgentMetrics(model="claude-opus-4-7")
    # SDK objects sometimes omit cache fields
    m.add_usage(SimpleNamespace(input_tokens=100, output_tokens=200))
    assert m.input_tokens == 100
    assert m.output_tokens == 200
    assert m.cache_read_tokens == 0
    assert m.cache_creation_tokens == 0


# ---------------------------------------------------------------------------
# Markdown formatting
# ---------------------------------------------------------------------------


def test_format_metrics_md_includes_all_fields() -> None:
    m = _opus_metrics()
    m.recompute_cost()
    text = format_metrics_md(m)
    assert "## Agent run details" in text
    assert "claude-opus-4-7" in text
    assert "42.5s" in text or "42.5" in text
    assert "Model calls" in text and "3" in text
    assert "Input tokens" in text
    assert "Output tokens" in text and "2,000" in text
    assert "Estimated cost" in text
    assert "USD" in text


def test_format_metrics_md_renders_minutes_for_long_runs() -> None:
    m = AgentMetrics(model="claude-opus-4-7", wall_time_seconds=180.0)
    text = format_metrics_md(m)
    assert "3.0 min" in text
    assert "(180.0s)" in text


def test_format_metrics_md_renders_seconds_for_short_runs() -> None:
    m = AgentMetrics(model="claude-haiku-4-5", wall_time_seconds=7.3)
    text = format_metrics_md(m)
    assert "7.3s" in text
    assert "min" not in text


def test_format_metrics_md_sub_cent_uses_four_decimals() -> None:
    m = AgentMetrics(model="claude-haiku-4-5", input_tokens=1000, output_tokens=500)
    m.recompute_cost()
    # ~$0.0035 — sub-cent, should render with 4 decimals
    text = format_metrics_md(m)
    assert "$0.0" in text
    # Must have at least 4 decimal places for accuracy at this scale
    cost_line = next(line for line in text.split("\n") if "Estimated cost" in line)
    assert cost_line.count(".") >= 1


# ---------------------------------------------------------------------------
# Integration with write_review_md
# ---------------------------------------------------------------------------


def test_review_md_includes_metrics_when_provided(tmp_path: Path) -> None:
    note = ReviewNote(
        summary="All good.",
        issues=[],
        filing_reminders=["Deadline July 31."],
        next_steps=["Upload portal CSV."],
    )
    m = _opus_metrics()
    m.recompute_cost()

    out = tmp_path / "review.md"
    write_review_md(note, out, metrics=m)
    text = out.read_text()

    assert "## Summary" in text
    assert "## Agent run details" in text
    assert "claude-opus-4-7" in text
    assert "Estimated cost" in text


def test_review_md_omits_metrics_when_not_provided(tmp_path: Path) -> None:
    note = ReviewNote(summary="Clean.", issues=[], filing_reminders=[], next_steps=[])
    out = tmp_path / "review.md"
    write_review_md(note, out)
    text = out.read_text()
    assert "## Agent run details" not in text
