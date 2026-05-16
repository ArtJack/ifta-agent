"""IFTA AI Agent — Claude-powered review/ask/chat over the pipeline output.

Public API:
    review(quarter, *, model=...)       → ReviewNote
    ask(question, *, quarter=..., model=...) → str
    chat_loop(*, model=...)             → None (interactive)
    write_review_md(note, path)         → Path

The full list of tools the agent can call lives in `agent.tools.ALL_TOOLS`.
The system prompt lives in `agent.prompts.SYSTEM_PROMPT`.
"""

from ifta.agent.metrics import AgentMetrics, format_metrics_md
from ifta.agent.runner import (
    DEFAULT_EFFORT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    ReviewNote,
    ask,
    chat_loop,
    format_review_item,
    review,
    review_note_from_payload,
    write_review_md,
)
from ifta.agent.tools import ALL_TOOLS

__all__ = [
    "ALL_TOOLS",
    "DEFAULT_EFFORT",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "AgentMetrics",
    "ReviewNote",
    "ask",
    "chat_loop",
    "format_metrics_md",
    "format_review_item",
    "review",
    "review_note_from_payload",
    "write_review_md",
]
