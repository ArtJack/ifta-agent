"""Back up the live ``data/`` directory (customer state) to dated snapshots.

The web service may be writing to ``data/web_jobs.db`` (WAL mode) at any moment,
so the SQLite file is copied through the online-backup API for a *consistent*
snapshot; everything else under ``data/`` is copied as-is. The result is a single
``ifta-data-<ts>.tar.gz`` under the backup directory — by default the AI-lab share,
which lives on a different machine than the Mac mini, so a Mac mini disk failure
cannot take the only copy with it. Old snapshots are pruned. See
``docs/IFTA_RUNBOOK.md``.

These are intentionally **plain** archives: protect them with full-disk encryption
on both the Mac mini and the lab box (the live data is plain there too).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_BACKUP_DIR = Path("/Volumes/DISK/AI/ifta-backups")
_DB_NAME = "web_jobs.db"
# Live SQLite sidecars (captured via the consistent db copy) + OS cruft we skip.
_SKIP = {_DB_NAME, f"{_DB_NAME}-wal", f"{_DB_NAME}-shm", ".DS_Store"}
_SNAPSHOT_GLOB = "ifta-data-*.tar.gz"


def backup_dir(dest: Path | None = None) -> Path:
    """Resolve the backup directory: explicit arg, then $IFTA_BACKUP_DIR, then default."""
    if dest is not None:
        return dest
    env = os.getenv("IFTA_BACKUP_DIR")
    return Path(env) if env else DEFAULT_BACKUP_DIR


def _hot_copy_sqlite(src_db: Path, dest_db: Path) -> None:
    """Consistent online backup of a (possibly live) SQLite database."""
    source = sqlite3.connect(src_db)
    try:
        dest = sqlite3.connect(dest_db)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def _stage(data_dir: Path, staging: Path) -> None:
    for entry in sorted(data_dir.iterdir()):
        if entry.name in _SKIP:
            continue
        target = staging / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, ignore=shutil.ignore_patterns(".DS_Store"))
        else:
            shutil.copy2(entry, target)
    db = data_dir / _DB_NAME
    if db.exists():
        _hot_copy_sqlite(db, staging / _DB_NAME)


def prune_snapshots(dest: Path, *, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` snapshots. Returns the ones removed."""
    snapshots = sorted(dest.glob(_SNAPSHOT_GLOB))
    stale = snapshots[:-keep] if keep > 0 else []
    for old in stale:
        old.unlink(missing_ok=True)
    return stale


def backup_data(project_root: Path, dest_dir: Path | None = None, *, keep: int = 14) -> Path:
    """Snapshot ``project_root/data`` to a dated tar.gz and prune old ones."""
    data_dir = project_root / "data"
    if not data_dir.exists():
        raise FileNotFoundError(f"no data directory to back up: {data_dir}")
    dest = backup_dir(dest_dir)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"backup directory {dest} is not writable ({exc}). Is the AI-lab share "
            "mounted? Set IFTA_BACKUP_DIR to override."
        ) from exc

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    snapshot = dest / f"ifta-data-{stamp}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "data"
        staging.mkdir()
        _stage(data_dir, staging)
        with tarfile.open(snapshot, "w:gz") as tar:
            tar.add(staging, arcname="data")

    prune_snapshots(dest, keep=keep)
    return snapshot


def list_snapshots(dest_dir: Path | None = None) -> list[Path]:
    """All snapshots in the backup directory, oldest first."""
    dest = backup_dir(dest_dir)
    if not dest.exists():
        return []
    return sorted(dest.glob(_SNAPSHOT_GLOB))


def restore_data(snapshot: Path, into: Path) -> Path:
    """Extract a snapshot's ``data/`` into ``into`` (which must be empty).

    Deliberately does NOT overwrite the live data directory — restore into a
    staging dir, verify it, then swap it in (see the runbook). Returns the path
    to the extracted ``data`` directory.
    """
    if not snapshot.exists():
        raise FileNotFoundError(f"snapshot not found: {snapshot}")
    into.mkdir(parents=True, exist_ok=True)
    if any(into.iterdir()):
        raise RuntimeError(f"refusing to restore into a non-empty directory: {into}")
    with tarfile.open(snapshot, "r:gz") as tar:
        tar.extractall(into, filter="data")  # 'data' filter blocks path traversal (py3.12+)
    extracted = into / "data"
    return extracted if extracted.exists() else into
