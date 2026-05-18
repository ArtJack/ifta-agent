"""Per-call agent execution context (path override for anonymous submissions).

The agent's tools normally resolve `inbox/<client>/<quarter>/` and
`outputs/<client>/<quarter>/` via `(PROJECT_ROOT, quarter, client)` — works for
the CLI and the Telegram bot, where each customer is a registered client.

Web submissions arrive anonymously: the data lives at
`data/web_submissions/<submission_id>/{inbox,outputs}/<quarter>/`. Before the
worker calls `agent.review`, it sets an `AgentExecutionContext` here; the
relevant tools check the override first and only fall back to the
client-registry path resolution when no override is set.

Telegram and CLI flows never set the override, so they're unaffected.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentExecutionContext:
    """Path overrides for one agent.review / agent.ask invocation."""

    inbox: Path
    output_dir: Path
    quarter: str  # canonical form, used to ensure overrides only apply to the matching quarter
    client_name: str | None = None


_override: ContextVar[AgentExecutionContext | None] = ContextVar(
    "ifta_agent_execution_context", default=None
)


def get() -> AgentExecutionContext | None:
    """Return the active override, or None if no override is set."""
    return _override.get()


def set_context(ctx: AgentExecutionContext) -> Token[AgentExecutionContext | None]:
    """Install an override; returns a token the caller must pass to reset()."""
    return _override.set(ctx)


def reset(token: Token[AgentExecutionContext | None]) -> None:
    """Restore the previous override (called in a try/finally)."""
    _override.reset(token)
