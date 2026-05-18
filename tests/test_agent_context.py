"""Tests for the agent execution-context override.

The override is the seam that lets the web worker drive the agent against
anonymous submission paths (`data/web_submissions/<sid>/…`) instead of the
conventional `inbox/<client>/<quarter>/` paths. We verify the helpers in
tools.py honor it AND that it cleans up on exit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ifta.agent import context as agent_context
from ifta.agent.tools import _build_client_context, _resolve_inbox


@pytest.fixture
def web_override(tmp_path: Path):
    """Install an override pointing at a tmp inbox/output for one test."""
    inbox = tmp_path / "inbox"
    out = tmp_path / "output"
    inbox.mkdir()
    out.mkdir()
    ctx = agent_context.AgentExecutionContext(
        inbox=inbox,
        output_dir=out,
        quarter="Q4-2025",
        client_name="Test Carrier LLC",
    )
    token = agent_context.set_context(ctx)
    yield ctx
    agent_context.reset(token)


def test_no_override_returns_none() -> None:
    assert agent_context.get() is None


def test_resolve_inbox_honors_override(web_override) -> None:
    assert _resolve_inbox("Q4-2025", client=None) == web_override.inbox


def test_resolve_inbox_falls_through_when_quarter_mismatches(
    web_override,
) -> None:
    """Override is scoped to a single quarter — other quarters use the
    normal client-registry path."""
    # Q1-2026 ≠ Q4-2025, so the override should be ignored.
    result = _resolve_inbox("Q1-2026", client=None)
    assert result != web_override.inbox


def test_build_client_context_returns_anonymous_when_overridden(
    web_override,
) -> None:
    ctx = _build_client_context("Q4-2025", client=None, inbox=web_override.inbox)
    assert ctx.client_id == "web"
    assert ctx.client_name == "Test Carrier LLC"
    assert ctx.source == "web"
    assert "anonymous" in ctx.notes.lower()


def test_reset_restores_clean_state(tmp_path: Path) -> None:
    ctx = agent_context.AgentExecutionContext(
        inbox=tmp_path,
        output_dir=tmp_path,
        quarter="Q1-2026",
    )
    token = agent_context.set_context(ctx)
    assert agent_context.get() is ctx
    agent_context.reset(token)
    assert agent_context.get() is None
