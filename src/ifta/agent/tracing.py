"""Lightweight, dependency-free tracing for the review agent.

A multi-step tool-using agent is a black box until you can see what it did. This
captures each model turn, the tools it called (with inputs), per-turn tokens, and
the final answer — so a run can be inspected and, later, span-evaluated ("did it
call the right tools, in a sensible order?").

No external tracing service. Tracing is opt-in via a context manager; when no trace
is active, `run_agent` does nothing extra (zero overhead, identical behavior). The
CLI's `ifta review --trace` opens a trace, runs the review, then saves + prints it.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _squish(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any]


@dataclass
class Turn:
    """One model turn inside the agent loop."""

    index: int
    text: str = ""
    thinking: bool = False
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Trace:
    label: str
    model: str
    started_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    turns: list[Turn] = field(default_factory=list)
    final_text: str = ""
    filing_status: str | None = None
    n_model_calls: int = 0
    n_tool_calls: int = 0
    wall_time_seconds: float = 0.0
    estimated_cost_usd: float = 0.0

    def record_turn(self, message: Any) -> None:
        """Extract a Turn from one SDK assistant message."""
        turn = Turn(index=len(self.turns))
        texts: list[str] = []
        for block in getattr(message, "content", None) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                texts.append(getattr(block, "text", "") or "")
            elif btype == "thinking":
                turn.thinking = True
            elif btype == "tool_use":
                turn.tool_calls.append(
                    ToolCall(
                        name=str(getattr(block, "name", "?")),
                        input=dict(getattr(block, "input", {}) or {}),
                    )
                )
        turn.text = _squish(" ".join(t for t in texts if t.strip()), 500)
        usage = getattr(message, "usage", None)
        if usage is not None:
            turn.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            turn.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        self.turns.append(turn)

    def close(self, *, final_text: str = "", metrics: Any = None, filing_status: str | None = None) -> None:
        self.final_text = _squish(final_text, 1000)
        self.filing_status = filing_status
        self.n_model_calls = len(self.turns)
        self.n_tool_calls = sum(len(t.tool_calls) for t in self.turns)
        if metrics is not None:
            self.wall_time_seconds = float(getattr(metrics, "wall_time_seconds", 0.0) or 0.0)
            self.estimated_cost_usd = float(getattr(metrics, "estimated_cost_usd", 0.0) or 0.0)

    def tool_sequence(self) -> list[str]:
        """The ordered tool names called — the basis for step/trajectory evaluation."""
        return [call.name for turn in self.turns for call in turn.tool_calls]


# --- opt-in capture (contextvar so run_agent stays untouched when inactive) ---

_active: ContextVar[Trace | None] = ContextVar("ifta_active_trace", default=None)


def current_trace() -> Trace | None:
    return _active.get()


@contextmanager
def traced(label: str, model: str):
    """Activate a Trace for the duration of the block; `run_agent` records into it."""
    trace = Trace(label=label, model=model)
    token = _active.set(trace)
    try:
        yield trace
    finally:
        _active.reset(token)


# --- persistence + view -----------------------------------------------------


def save_trace(trace: Trace, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(trace), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def render_trace(trace: Trace) -> str:
    lines = [
        f"Trace: {trace.label}  [{trace.model}]",
        f"  {trace.n_model_calls} model calls · {trace.n_tool_calls} tool calls · "
        f"{trace.wall_time_seconds:.1f}s · ${trace.estimated_cost_usd:.4f}",
        f"  tools: {' → '.join(trace.tool_sequence()) or '(none)'}",
        "",
    ]
    for turn in trace.turns:
        bits: list[str] = []
        if turn.thinking:
            bits.append("thinking")
        for call in turn.tool_calls:
            args = ", ".join(f"{k}={v!r}" for k, v in call.input.items())
            bits.append(f"{call.name}({_squish(args, 80)})")
        head = f"  turn {turn.index} [{turn.input_tokens}+{turn.output_tokens} tok]"
        lines.append(f"{head}: {' · '.join(bits)}" if bits else head)
        if turn.text:
            lines.append(f'      "{_squish(turn.text, 200)}"')
    if trace.filing_status:
        lines += ["", f"  filing_status: {trace.filing_status}"]
    if trace.final_text:
        lines += ["", f"  final: {_squish(trace.final_text, 300)}"]
    return "\n".join(lines)
