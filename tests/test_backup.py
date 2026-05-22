"""Tests for the raw-input backup module."""

from __future__ import annotations

from pathlib import Path

from ifta.backup import archive_inputs, default_archive_root


def _make_inbox(tmp_path: Path) -> Path:
    inbox = tmp_path / "subs" / "sid12345678" / "inbox" / "Q1-2026"
    inbox.mkdir(parents=True)
    (inbox / "mileage.csv").write_text("truck,state,miles\nT1,KY,1000\n", encoding="utf-8")
    (inbox / "fuel.csv").write_text("truck,state,gallons\nT1,KY,150\n", encoding="utf-8")
    return inbox


def test_archive_inputs_copies_all_files(tmp_path: Path) -> None:
    inbox = _make_inbox(tmp_path)
    archive_root = tmp_path / "backups"
    copied = archive_inputs(
        inbox,
        archive_root=archive_root,
        quarter="Q1-2026",
        company="BLA BLA Transportation",
        submission_id="sid12345678",
    )
    assert len(copied) == 2
    names = {p.name for p in copied}
    assert names == {"mileage.csv", "fuel.csv"}
    # Originals untouched; copies have the same content.
    assert inbox.exists()
    for p in copied:
        assert p.exists()
        assert p.read_text(encoding="utf-8") == (inbox / p.name).read_text(encoding="utf-8")
    # Archived under quarter, in a folder tagged with the company slug.
    dest = copied[0].parent
    assert dest.parent.name == "Q1-2026"
    assert "bla-bla-transportation" in dest.name
    assert "sid12345" in dest.name


def test_archive_inputs_missing_inbox_returns_empty(tmp_path: Path) -> None:
    copied = archive_inputs(
        tmp_path / "nope",
        archive_root=tmp_path / "backups",
        quarter="Q1-2026",
        company=None,
        submission_id="x",
    )
    assert copied == []


def test_archive_inputs_handles_missing_company(tmp_path: Path) -> None:
    inbox = _make_inbox(tmp_path)
    copied = archive_inputs(
        inbox,
        archive_root=tmp_path / "backups",
        quarter="Q1-2026",
        company=None,
        submission_id="sid12345678",
    )
    assert len(copied) == 2
    assert "unknown" in copied[0].parent.name


def test_archive_inputs_skips_subdirectories(tmp_path: Path) -> None:
    inbox = _make_inbox(tmp_path)
    (inbox / "nested").mkdir()
    (inbox / "nested" / "ignored.csv").write_text("x", encoding="utf-8")
    copied = archive_inputs(
        inbox,
        archive_root=tmp_path / "backups",
        quarter="Q1-2026",
        company="ACME",
        submission_id="sid12345678",
    )
    assert {p.name for p in copied} == {"mileage.csv", "fuel.csv"}


def test_default_archive_root_is_sibling_of_submissions(tmp_path: Path) -> None:
    submissions = tmp_path / "data" / "web_submissions"
    assert default_archive_root(submissions) == tmp_path / "data" / "backups"
