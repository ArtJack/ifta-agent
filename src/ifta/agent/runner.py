"""Agent runner — model invocation, review, ask, chat loop, output writers.

Public functions (`review`, `ask`, `chat_loop`) are re-exported from
`ifta.agent`. The Claude API key is loaded from `.env` at import time.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import anthropic
from dotenv import load_dotenv

from ifta.agent.metrics import AgentMetrics
from ifta.agent.prompts import REVIEW_PROMPT_TEMPLATE, SYSTEM_PROMPT
from ifta.agent.tools import ALL_TOOLS
from ifta.client import load_client_context

PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_EFFORT = "medium"
DEFAULT_MAX_TOKENS = {"review": 4096, "ask": 2048, "chat": 4096}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


ReviewItem = str | dict[str, Any]


@dataclass
class ReviewNote:
    summary: str
    issues: list[ReviewItem]
    filing_reminders: list[ReviewItem]
    next_steps: list[ReviewItem]


def _string_or_empty(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _as_review_items(value: Any) -> list[ReviewItem]:
    if value is None:
        return []
    raw_items = value if isinstance(value, list) else [value]
    items: list[ReviewItem] = []
    for item in raw_items:
        if item is None:
            continue
        if isinstance(item, dict):
            items.append(item)
        else:
            text = _string_or_empty(item)
            if text:
                items.append(text)
    return items


def review_note_from_payload(payload: dict[str, Any]) -> ReviewNote:
    """Normalize model JSON into a ReviewNote without assuming exact item shape."""
    return ReviewNote(
        summary=_string_or_empty(payload.get("summary")),
        issues=_as_review_items(payload.get("issues")),
        filing_reminders=_as_review_items(payload.get("filing_reminders")),
        next_steps=_as_review_items(payload.get("next_steps")),
    )


def format_review_item(item: ReviewItem, *, checkbox: bool = False) -> str:
    """Render string or structured review item as clean, client-facing text."""
    if isinstance(item, str):
        text = item.strip()
    else:
        severity = _string_or_empty(item.get("severity"))
        title = ""
        for key in ("id", "code", "item", "todo", "title"):
            title = _string_or_empty(item.get(key))
            if title:
                break
        detail = ""
        for key in ("detail", "description", "message", "note", "action"):
            detail = _string_or_empty(item.get(key))
            if detail:
                break
        text = f"{title}: {detail}" if title and detail and title != detail else detail or title
        if not text:
            text = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if severity:
            text = f"[{severity}] {text}"
    text = " ".join(text.split())
    return f"[ ] {text}" if checkbox else text


# ---------------------------------------------------------------------------
# Client + per-model parameters
# ---------------------------------------------------------------------------


def _client() -> anthropic.Anthropic:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Create ifta_pipeline/.env with:\n"
            "  ANTHROPIC_API_KEY=sk-ant-api03-...\n"
            "Get a key at https://console.anthropic.com/settings/keys"
        )
    return anthropic.Anthropic()


def _model_kwargs(model: str, effort: str = DEFAULT_EFFORT) -> dict[str, Any]:
    """Per-model parameter quirks for the SDK."""
    if model.startswith(("claude-opus-4-7", "claude-sonnet-4-6")):
        return {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
        }
    if model.startswith("claude-haiku-4-5"):
        return {"thinking": {"type": "disabled"}}
    return {}


# ---------------------------------------------------------------------------
# Core invocation
# ---------------------------------------------------------------------------


def run_agent(
    user_message: str,
    *,
    model: str = DEFAULT_MODEL,
    history: list[dict[str, Any]] | None = None,
    max_tokens: int = 4096,
    effort: str = DEFAULT_EFFORT,
) -> tuple[str, list[dict[str, Any]], AgentMetrics]:
    """Run one agent turn (tool runner handles tool calls internally).

    Returns (final_text, updated_history, metrics).
    """
    client = _client()
    messages: list[dict[str, Any]] = (history or []) + [{"role": "user", "content": user_message}]
    metrics = AgentMetrics(model=model)
    start = time.monotonic()

    tool_runner = cast(Any, client.beta.messages.tool_runner)
    runner = tool_runner(
        model=model,
        max_tokens=max_tokens,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=ALL_TOOLS,
        messages=messages,
        **_model_kwargs(model, effort=effort),
    )

    final_text_parts: list[str] = []
    final_message = None
    for message in runner:
        final_message = message
        metrics.add_usage(getattr(message, "usage", None))
        for block in message.content:
            if block.type == "text":
                final_text_parts.append(block.text)

    metrics.wall_time_seconds = round(time.monotonic() - start, 2)
    metrics.recompute_cost()

    final_text = "\n".join(t for t in final_text_parts if t.strip())
    new_history = messages
    if final_message is not None:
        new_history = [*messages, {"role": "assistant", "content": final_message.content}]
    return final_text, new_history, metrics


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def review(
    quarter: str,
    *,
    client: str | None = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int | None = None,
    effort: str = DEFAULT_EFFORT,
) -> tuple[ReviewNote, AgentMetrics]:
    """Produce a structured pre-filing review for one quarter.

    Returns (review_note, metrics).
    """
    client_context = load_client_context(PROJECT_ROOT, quarter, client=client)
    prompt = REVIEW_PROMPT_TEMPLATE.format(
        quarter=quarter,
        client_context=json.dumps(client_context.to_prompt_dict(), indent=2),
    )
    text, _, metrics = run_agent(
        prompt,
        model=model,
        max_tokens=max_tokens or DEFAULT_MAX_TOKENS["review"],
        effort=effort,
    )
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError(f"Agent did not return JSON. Raw response:\n{text}")
    payload = json.loads(text[start : end + 1])
    return review_note_from_payload(payload), metrics


def ask(
    question: str,
    *,
    quarter: str | None = None,
    client: str | None = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int | None = None,
    effort: str = DEFAULT_EFFORT,
) -> str:
    """One-shot Q&A. If a quarter is given, the agent focuses on it."""
    prefix = ""
    if quarter or client:
        context_quarter = quarter or "unknown"
        client_context = load_client_context(PROJECT_ROOT, context_quarter, client=client)
        prefix = (
            f"(Context: focus on quarter {quarter or 'unspecified'} unless otherwise stated. "
            f"Client context: {json.dumps(client_context.to_prompt_dict())})\n\n"
        )
    text, _, _ = run_agent(
        prefix + question,
        model=model,
        max_tokens=max_tokens or DEFAULT_MAX_TOKENS["ask"],
        effort=effort,
    )
    return text


def chat_loop(
    model: str = DEFAULT_MODEL,
    max_tokens: int | None = None,
    effort: str = DEFAULT_EFFORT,
) -> None:
    """Interactive multi-turn chat. Prints to stdout."""
    from rich.console import Console
    from rich.markdown import Markdown

    console = Console()
    console.rule(f"[bold]IFTA Agent — {model}")
    console.print(
        "[dim]Type 'exit' or Ctrl-C to quit. "
        "Agent has tools to read returns, regulations, history.[/dim]"
    )

    history: list[dict[str, Any]] = []
    while True:
        try:
            user = console.input("\n[bold cyan]You:[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            return
        if not user or user.lower() in ("exit", "quit", ":q"):
            return
        try:
            text, history, _ = run_agent(
                user,
                model=model,
                history=history,
                max_tokens=max_tokens or DEFAULT_MAX_TOKENS["chat"],
                effort=effort,
            )
        except Exception as e:
            console.print(f"[red]Error:[/] {e}")
            continue
        console.print("\n[bold green]Agent:[/]")
        console.print(Markdown(text))


# ---------------------------------------------------------------------------
# Output writer
# ---------------------------------------------------------------------------


def write_review_md(
    note: ReviewNote, out_path: Path, *, metrics: AgentMetrics | None = None
) -> Path:
    """Write a ReviewNote to a Markdown file with optional agent-run metrics."""
    from ifta.agent.metrics import format_metrics_md

    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# IFTA Review Note", "", "## Summary", note.summary, ""]
    if note.issues:
        lines += ["## Issues", *(f"- {format_review_item(x)}" for x in note.issues), ""]
    if note.filing_reminders:
        lines += [
            "## Filing reminders",
            *(f"- {format_review_item(x)}" for x in note.filing_reminders),
            "",
        ]
    if note.next_steps:
        lines += [
            "## Next steps",
            *(f"- {format_review_item(x, checkbox=True)}" for x in note.next_steps),
            "",
        ]
    if metrics is not None:
        lines += [format_metrics_md(metrics), ""]
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path
