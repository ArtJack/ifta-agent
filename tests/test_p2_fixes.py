"""Regression tests for the three P2 defects shipped 2026-05-16.

D-011: ifta onboard refuses alias/id collisions
D-014: review JSON extractor strips code fences + tolerates extra prose
D-015: write_review_md archives existing files instead of overwriting
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from ifta.agent.runner import (
    ReviewNote,
    _extract_review_json,
    write_review_md,
)
from ifta.cli import main

# ---------------------------------------------------------------------------
# D-011 — alias collision detection in onboard
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the CLI at a tmp project root holding one synthetic client.

    Keeps the onboard collision tests hermetic: they no longer depend on (or
    write into) the real data/clients registry. The committed-free fixture is
    'acme_freight' with alias 'acme'.
    """
    import json

    from ifta import cli
    from ifta import client as client_mod

    cdir = tmp_path / "data" / "clients" / "acme_freight"
    cdir.mkdir(parents=True)
    (cdir / "client.json").write_text(
        json.dumps(
            {
                "client_id": "acme_freight",
                "name": "ACME FREIGHT",
                "aliases": ["acme"],
                "active": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    client_mod.reload_registry(tmp_path)
    yield tmp_path
    client_mod.reload_registry(tmp_path)  # drop the tmp registry from the cache


def test_onboard_refuses_existing_id(runner: CliRunner, isolated_registry) -> None:
    """`ifta onboard acme_freight` must fail because the id is taken."""
    result = runner.invoke(main, ["onboard", "acme_freight"])
    assert result.exit_code != 0, result.output


def test_onboard_refuses_alias_collision(runner: CliRunner, isolated_registry) -> None:
    """`ifta onboard acme` must fail because 'acme' is an alias of acme_freight."""
    result = runner.invoke(main, ["onboard", "acme"])
    assert result.exit_code != 0, result.output
    assert "acme_freight" in result.output
    assert "alias" in result.output.lower() or "resolves" in result.output.lower()


def test_onboard_refuses_alias_case_insensitive(runner: CliRunner, isolated_registry) -> None:
    """Aliases are normalized — 'ACME', 'Acme', 'acme' all collide."""
    result = runner.invoke(main, ["onboard", "ACME"])
    assert result.exit_code != 0, result.output
    assert "acme_freight" in result.output


# ---------------------------------------------------------------------------
# D-014 — review JSON extraction handles fences + extra prose
# ---------------------------------------------------------------------------


def test_extract_review_json_plain() -> None:
    payload = _extract_review_json('{"summary": "ok", "issues": []}')
    assert payload == {"summary": "ok", "issues": []}


def test_extract_review_json_with_preamble() -> None:
    text = (
        "Here is the review note you asked for:\n\n"
        '{"summary": "ok", "issues": []}\n\n'
        "Let me know if you need anything else."
    )
    payload = _extract_review_json(text)
    assert payload["summary"] == "ok"


def test_extract_review_json_with_json_code_fence() -> None:
    text = '```json\n{"summary": "fenced", "issues": ["a", "b"]}\n```'
    payload = _extract_review_json(text)
    assert payload["summary"] == "fenced"
    assert payload["issues"] == ["a", "b"]


def test_extract_review_json_with_plain_code_fence() -> None:
    text = '```\n{"summary": "plain fence", "issues": []}\n```'
    payload = _extract_review_json(text)
    assert payload["summary"] == "plain fence"


def test_extract_review_json_raises_when_no_object() -> None:
    with pytest.raises(ValueError, match="no JSON object found"):
        _extract_review_json("no braces here at all")


# ---------------------------------------------------------------------------
# D-015 — write_review_md archives existing files
# ---------------------------------------------------------------------------


def _make_note(summary: str) -> ReviewNote:
    return ReviewNote(summary=summary, issues=[], filing_reminders=[], next_steps=[])


def test_write_review_md_creates_when_missing(tmp_path: Path) -> None:
    out = tmp_path / "review_note.md"
    write_review_md(_make_note("first"), out)
    assert out.exists()
    assert "first" in out.read_text(encoding="utf-8")
    siblings = list(tmp_path.glob("review_note.archive.*"))
    assert siblings == []


def test_write_review_md_archives_existing(tmp_path: Path) -> None:
    out = tmp_path / "review_note.md"
    write_review_md(_make_note("first run"), out)
    write_review_md(_make_note("second run"), out)

    assert "second run" in out.read_text(encoding="utf-8")
    archives = list(tmp_path.glob("review_note.archive.*.md"))
    assert len(archives) == 1, archives
    assert "first run" in archives[0].read_text(encoding="utf-8")


def test_write_review_md_overwrite_flag_skips_archive(tmp_path: Path) -> None:
    out = tmp_path / "review_note.md"
    write_review_md(_make_note("first"), out)
    write_review_md(_make_note("second"), out, overwrite=True)

    assert "second" in out.read_text(encoding="utf-8")
    assert list(tmp_path.glob("review_note.archive.*")) == []


def test_write_review_md_multiple_archives(tmp_path: Path) -> None:
    """Three writes → one current + two archives, each with prior content."""
    out = tmp_path / "review_note.md"
    import time

    write_review_md(_make_note("v1"), out)
    time.sleep(1.05)  # second-resolution timestamps must differ
    write_review_md(_make_note("v2"), out)
    time.sleep(1.05)
    write_review_md(_make_note("v3"), out)

    assert "v3" in out.read_text(encoding="utf-8")
    archives = sorted(tmp_path.glob("review_note.archive.*.md"))
    assert len(archives) == 2, archives
    texts = [a.read_text(encoding="utf-8") for a in archives]
    # one is v1, one is v2 (order depends on archive timestamps, both present)
    joined = "\n".join(texts)
    assert "v1" in joined and "v2" in joined


# ---------------------------------------------------------------------------
# Smoke: ensure suite still imports cleanly
# ---------------------------------------------------------------------------


def test_review_note_dataclass() -> None:
    """Sanity — ReviewNote builds the way write_review_md expects."""
    note = _make_note("hi")
    assert note.summary == "hi"
    assert note.issues == []
