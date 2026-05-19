"""Agent runner — model invocation, review, ask, chat loop, output writers.

Public functions (`review`, `ask`, `chat_loop`) are re-exported from
`ifta.agent`. The Claude API key is loaded from `.env` at import time.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import anthropic
from dotenv import load_dotenv

from ifta.agent.metrics import AgentMetrics
from ifta.agent.prompts import REVIEW_PROMPT_TEMPLATE, SYSTEM_PROMPT
from ifta.agent.tools import ALL_TOOLS
from ifta.client import load_client_context, quarter_key
from ifta.review_packet import build_review_packet

PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_MODEL = "claude-sonnet-4-6"
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
    filing_status: str | None = None
    filing_status_reasons: list[str] = field(default_factory=list)


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


def _as_strings(value: Any) -> list[str]:
    if value is None:
        return []
    raw_items = value if isinstance(value, list) else [value]
    return [text for item in raw_items if (text := _string_or_empty(item))]


def review_note_from_payload(payload: dict[str, Any]) -> ReviewNote:
    """Normalize model JSON into a ReviewNote without assuming exact item shape."""
    filing_status = _string_or_empty(payload.get("filing_status") or payload.get("status")) or None
    return ReviewNote(
        summary=_string_or_empty(payload.get("summary")),
        issues=_as_review_items(payload.get("issues")),
        filing_reminders=_as_review_items(payload.get("filing_reminders")),
        next_steps=_as_review_items(payload.get("next_steps")),
        filing_status=filing_status,
        filing_status_reasons=_as_strings(
            payload.get("filing_status_reasons") or payload.get("status_reasons")
        ),
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
        for key in (
            "claim",
            "detail",
            "description",
            "message",
            "note",
            "recommended_action",
            "action",
        ):
            detail = _string_or_empty(item.get(key))
            if detail:
                break
        text = f"{title}: {detail}" if title and detail and title != detail else detail or title
        if item.get("evidence") is not None:
            text = (
                f"{text} Evidence: "
                f"{json.dumps(item['evidence'], ensure_ascii=False, sort_keys=True)}"
            ).strip()
        filing_impact = _string_or_empty(item.get("filing_impact"))
        if filing_impact:
            text = f"{text} Impact: {filing_impact}".strip()
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


_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def _extract_review_json(text: str) -> dict[str, Any]:
    """Pull the JSON object out of the agent's reply.

    Models sometimes wrap output in ```json … ``` fences despite instructions,
    or stick stray prose around the object. Strip fences first, then fall back
    to the {…} braces. Raises ValueError if no valid object is recoverable.
    """
    candidate = text
    fenced = _CODE_FENCE_RE.search(text)
    if fenced:
        candidate = fenced.group(1)

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object found in:\n{text}")
    return json.loads(candidate[start : end + 1])


def review(
    quarter: str,
    *,
    client: str | None = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int | None = None,
    effort: str = DEFAULT_EFFORT,
    inbox_dir: Path | None = None,
    output_dir: Path | None = None,
    client_name: str | None = None,
) -> tuple[ReviewNote, AgentMetrics]:
    """Produce a structured pre-filing review for one quarter.

    Returns (review_note, metrics). Retries the agent call once if the first
    response can't be parsed as JSON (caught ~10% of intermittent failures in
    QA — models occasionally produce nearly-valid JSON with an unescaped char).

    For anonymous web submissions, pass `inbox_dir` and `output_dir` to point
    the agent's tools at the submission's own files (under
    `data/web_submissions/<sid>/`) rather than at the standard
    `inbox/<client>/<quarter>/` paths. `client_name` is shown to the agent
    so it has something more useful than "Anonymous web submission" when
    the customer provided their carrier name.
    """
    from ifta.agent import context as agent_context

    qkey = quarter_key(quarter)
    override_token: object | None = None
    if inbox_dir is not None or output_dir is not None:
        if inbox_dir is None or output_dir is None:
            raise ValueError(
                "inbox_dir and output_dir must both be set when overriding paths."
            )
        override_token = agent_context.set_context(
            agent_context.AgentExecutionContext(
                inbox=inbox_dir,
                output_dir=output_dir,
                quarter=qkey,
                client_name=client_name,
            )
        )

    try:
        if override_token is not None:
            client_context_dict = {
                "client_id": None,
                "client_name": client_name or "Anonymous web submission",
                "source": "web",
                "notes": (
                    "First-time anonymous web submission. No historical "
                    "filings or carrier profile available — review the "
                    "current quarter on its own merits."
                ),
            }
        else:
            client_context_dict = load_client_context(
                PROJECT_ROOT, quarter, client=client
            ).to_prompt_dict()
        return _run_review_with_retry(
            quarter=quarter,
            client_context_dict=client_context_dict,
            model=model,
            max_tokens=max_tokens,
            effort=effort,
        )
    finally:
        if override_token is not None:
            agent_context.reset(override_token)  # type: ignore[arg-type]


def _run_review_with_retry(
    *,
    quarter: str,
    client_context_dict: dict,
    model: str,
    max_tokens: int | None,
    effort: str,
) -> tuple[ReviewNote, AgentMetrics]:
    from ifta.agent.tools import _load_quarter_full

    data, _, ret, findings, client_context = _load_quarter_full(quarter, client_context_dict.get("client_id"))
    review_packet = build_review_packet(data, ret, findings, client_context)
    prompt = REVIEW_PROMPT_TEMPLATE.format(
        quarter=quarter,
        client_context=json.dumps(client_context_dict, indent=2),
        review_packet=json.dumps(review_packet, indent=2),
    )
    text, _, metrics = run_agent(
        prompt,
        model=model,
        max_tokens=max_tokens or DEFAULT_MAX_TOKENS["review"],
        effort=effort,
    )
    try:
        payload = _extract_review_json(text)
    except (ValueError, json.JSONDecodeError) as first_err:
        retry_prompt = (
            prompt
            + "\n\nIMPORTANT: Your previous response was not valid JSON. Return "
            "ONLY the JSON object specified — no markdown fences, no preamble, "
            "no trailing commentary. Escape any quotes or newlines inside string "
            "values."
        )
        retry_text, _, retry_metrics = run_agent(
            retry_prompt,
            model=model,
            max_tokens=max_tokens or DEFAULT_MAX_TOKENS["review"],
            effort=effort,
        )
        metrics.merge(retry_metrics)
        try:
            payload = _extract_review_json(retry_text)
        except (ValueError, json.JSONDecodeError) as retry_err:
            raise RuntimeError(
                f"Agent failed to produce valid JSON after retry.\n"
                f"First error: {first_err}\nRetry error: {retry_err}\n"
                f"Last response:\n{retry_text}"
            ) from retry_err
    note = review_note_from_payload(payload)
    _enforce_deterministic_filing_status(note, review_packet["filing_status"])
    return note, metrics


def _enforce_deterministic_filing_status(
    note: ReviewNote, filing_status: dict[str, Any]
) -> None:
    """Make the deterministic filing gate authoritative over model wording."""
    expected = _string_or_empty(filing_status.get("status"))
    reasons = _as_strings(filing_status.get("reasons"))
    if not expected:
        # The packet must always carry a status (DO_NOT_FILE / READY_WITH_WARNINGS
        # / READY_TO_FILE). An empty value means the packet is malformed; fail
        # loud rather than letting the model's status pass through unchecked.
        raise ValueError("review_packet.filing_status.status is empty")

    model_status = _string_or_empty(note.filing_status)
    if model_status and model_status != expected:
        note.issues.insert(
            0,
            {
                "severity": "error" if expected == "DO_NOT_FILE" else "warning",
                "code": "FILING_STATUS_OVERRIDE",
                "claim": f"Model returned {model_status}, but deterministic status is {expected}.",
                "evidence": {"source": "review_packet.filing_status", "value": filing_status},
                "recommended_action": "Use the deterministic filing status.",
                "filing_impact": "The model cannot weaken filing-readiness gates.",
            },
        )
    note.filing_status = expected
    note.filing_status_reasons = reasons


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
    note: ReviewNote,
    out_path: Path,
    *,
    metrics: AgentMetrics | None = None,
    overwrite: bool = False,
) -> Path:
    """Write a ReviewNote to a Markdown file with optional agent-run metrics.

    If `out_path` already exists and `overwrite` is False (the default), the
    existing file is archived to `<stem>.archive.<ISO-timestamp><suffix>` in
    the same directory before the new file is written. This stops re-runs from
    silently destroying the previous (possibly already-delivered) review note.
    Pass `overwrite=True` to keep the old behaviour.
    """
    from datetime import datetime

    from ifta.agent.metrics import format_metrics_md

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists() and not overwrite:
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        archive = out_path.with_name(
            f"{out_path.stem}.archive.{ts}{out_path.suffix}"
        )
        out_path.rename(archive)
    lines = ["# IFTA Review Note", "", "## Summary", note.summary, ""]
    if note.filing_status:
        lines += ["## Filing status", note.filing_status]
        if note.filing_status_reasons:
            lines += ["", *[f"- {reason}" for reason in note.filing_status_reasons]]
        lines.append("")
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
