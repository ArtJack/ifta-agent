"""Tests for the data backup/restore mechanism — offline, deterministic."""

import sqlite3
import tarfile

import pytest

from ifta.backup import backup_data, list_snapshots, prune_snapshots, restore_data


def _make_data(root):
    data = root / "data"
    (data / "clients").mkdir(parents=True)
    (data / "clients" / "x.json").write_text('{"id": "x"}')
    (data / "telegram_access.json").write_text("{}")
    conn = sqlite3.connect(data / "web_jobs.db")
    conn.execute("CREATE TABLE submissions (id TEXT, status TEXT)")
    conn.execute("INSERT INTO submissions VALUES ('s1', 'done')")
    conn.commit()
    conn.close()
    return data


def test_backup_captures_a_consistent_db_and_files(tmp_path):
    root = tmp_path / "proj"
    _make_data(root)
    snap = backup_data(root, tmp_path / "backups", keep=14)
    assert snap.exists() and snap.name.endswith(".tar.gz")

    out = tmp_path / "out"
    out.mkdir()
    with tarfile.open(snap) as tar:
        tar.extractall(out, filter="data")

    restored_db = out / "data" / "web_jobs.db"
    assert restored_db.exists()
    conn = sqlite3.connect(restored_db)
    rows = conn.execute("SELECT id, status FROM submissions").fetchall()
    conn.close()
    assert rows == [("s1", "done")]  # hot-backup preserved the row
    assert (out / "data" / "clients" / "x.json").exists()
    assert (out / "data" / "telegram_access.json").exists()


def test_backup_skips_wal_sidecars_and_cruft(tmp_path):
    root = tmp_path / "proj"
    data = _make_data(root)
    (data / "web_jobs.db-wal").write_text("walcruft")
    (data / ".DS_Store").write_text("x")
    snap = backup_data(root, tmp_path / "b", keep=14)
    with tarfile.open(snap) as tar:
        names = tar.getnames()
    assert "data/web_jobs.db" in names
    assert "data/web_jobs.db-wal" not in names
    assert "data/.DS_Store" not in names


def test_prune_keeps_newest_n(tmp_path):
    dest = tmp_path / "b"
    dest.mkdir()
    for i in range(5):
        (dest / f"ifta-data-2026010{i}T000000Z.tar.gz").write_text("x")
    pruned = prune_snapshots(dest, keep=2)
    assert len(pruned) == 3
    assert len(list_snapshots(dest)) == 2


def test_backup_prunes_to_keep(tmp_path):
    dest = tmp_path / "b"
    dest.mkdir()
    for i in range(3):
        (dest / f"ifta-data-2020010{i}T000000Z.tar.gz").write_text("old")
    root = tmp_path / "proj"
    _make_data(root)
    backup_data(root, dest, keep=2)
    assert len(list_snapshots(dest)) == 2


def test_restore_extracts_and_refuses_nonempty_target(tmp_path):
    root = tmp_path / "proj"
    _make_data(root)
    snap = backup_data(root, tmp_path / "b", keep=14)
    into = tmp_path / "restored"
    extracted = restore_data(snap, into)
    assert (extracted / "web_jobs.db").exists()
    with pytest.raises(RuntimeError):  # never clobber an existing dir
        restore_data(snap, into)


def test_missing_data_dir_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_data(tmp_path / "nope", tmp_path / "b")
