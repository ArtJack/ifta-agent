"""Agent run metrics — wall time, token usage, estimated USD cost.

The Claude SDK returns a `usage` block on every message. When the
tool-runner iterates through multiple model calls, we sum the usage
across them so the final number reflects the full agent run.

Pricing snapshot (per 1M tokens, USD) — update when Anthropic publishes
new rates:
    https://platform.claude.com/docs/en/pricing
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Per-1M-token USD pricing. Cache pricing follows Anthropic's standard
# multipliers: reads = 0.1× input, writes (5-min TTL) = 1.25× input.
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7": {"input": 5.0, "output": 25.0},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}

CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


@dataclass
class AgentMetrics:
    """Aggregated usage + cost for one agent invocation."""

    model: str
    wall_time_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    estimated_cost_usd: float = 0.0
    n_model_calls: int = 0

    def add_usage(self, usage: Any) -> None:
        """Accumulate one message's usage object into this metrics totals."""
        if usage is None:
            return
        self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        self.cache_read_tokens += int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        self.cache_creation_tokens += int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        self.n_model_calls += 1

    def merge(self, other: "AgentMetrics") -> None:
        """Add another run's totals into this one (e.g. for parse-retry path)."""
        self.wall_time_seconds += other.wall_time_seconds
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.n_model_calls += other.n_model_calls
        self.recompute_cost()

    def recompute_cost(self) -> None:
        """Recalculate estimated_cost_usd from the current token totals."""
        pricing = MODEL_PRICING.get(self.model)
        if pricing is None:
            self.estimated_cost_usd = 0.0
            return
        per_token_in = pricing["input"] / 1_000_000
        per_token_out = pricing["output"] / 1_000_000
        cost = (
            self.input_tokens * per_token_in
            + self.output_tokens * per_token_out
            + self.cache_read_tokens * per_token_in * CACHE_READ_MULTIPLIER
            + self.cache_creation_tokens * per_token_in * CACHE_WRITE_MULTIPLIER
        )
        # Round to 4 decimal places (cents-and-bits)
        self.estimated_cost_usd = round(cost, 4)


def format_metrics_md(m: AgentMetrics) -> str:
    """Render an AgentMetrics block as Markdown for the review note footer."""
    if m.wall_time_seconds >= 60:
        time_str = f"{m.wall_time_seconds / 60:.1f} min ({m.wall_time_seconds:.1f}s)"
    else:
        time_str = f"{m.wall_time_seconds:.1f}s"

    cost_str = (
        f"${m.estimated_cost_usd:.4f}"
        if m.estimated_cost_usd < 0.01
        else f"${m.estimated_cost_usd:.2f}"
    )
    total_in = m.input_tokens + m.cache_read_tokens + m.cache_creation_tokens

    lines = [
        "## Agent run details",
        "",
        f"- **Model:** `{m.model}`",
        f"- **Wall time:** {time_str}",
        f"- **Model calls:** {m.n_model_calls}",
        f"- **Input tokens:** {total_in:,} "
        f"(uncached {m.input_tokens:,} · cached-read {m.cache_read_tokens:,} · "
        f"cache-write {m.cache_creation_tokens:,})",
        f"- **Output tokens:** {m.output_tokens:,}",
        f"- **Estimated cost:** {cost_str} USD",
    ]
    return "\n".join(lines)
