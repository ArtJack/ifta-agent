"""Back up the live ``data/`` directory (customer state) to dated snapshots.

Whatever holds job state is captured *consistently* alongside the files:

* **SQLite** (Mac mini) — ``data/web_jobs.db`` may be mid-write in WAL mode, so
  it is copied through the online-backup API rather than as bytes on disk.
* **Postgres** (Oracle box / Azure) — when ``IFTA_WEB_DB_URL`` is set the job
  state does not live under ``data/`` at all, so a file-only archive would
  restore submissions with no jobs. ``pg_dump --format=custom`` writes
  ``web_jobs.dump`` into the same archive.

Everything else under ``data/`` is copied as-is. The result is a single
``ifta-data-<ts>.tar.gz`` under the backup directory, and — when R2 is
configured — a copy in the bucket. Both sides are pruned to the newest
``DEFAULT_KEEP`` archives, so a new snapshot displaces the oldest instead of
accumulating. See ``docs/ORACLE.md`` and ``docs/IFTA_RUNBOOK.md``.

These are intentionally **plain** archives of real customer PII: protect them
with full-disk encryption on the host and keep the R2 bucket private.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from ifta.backup_remote import RemoteConfig, prune_remote, upload_snapshot

DEFAULT_BACKUP_DIR = Path("/Volumes/DISK/AI/ifta-backups")
# Deliberately small: these are full snapshots of the same small dataset, so
# holding a long tail buys nothing. Each run drops the oldest beyond this.
DEFAULT_KEEP = 3
_DB_NAME = "web_jobs.db"
_PG_DUMP_NAME = "web_jobs.dump"
# Live SQLite sidecars (captured via the consistent db copy) + OS cruft we skip.
_SKIP = {_DB_NAME, f"{_DB_NAME}-wal", f"{_DB_NAME}-shm", ".DS_Store"}
_SNAPSHOT_GLOB = "ifta-data-*.tar.gz"
# pg_dump can hang forever on an unreachable host; a backup that never returns
# blocks the systemd timer and looks like success to anything watching exit 0.
_PG_DUMP_TIMEOUT_S = 900


@dataclass(frozen=True)
class BackupResult:
    """What one ``run_backup`` produced, for logging and for the CLI to print."""

    snapshot: Path
    local_kept: int
    included_pg_dump: bool
    remote_key: str | None = None
    remote_kept: int | None = None
    remote_pruned: tuple[str, ...] = ()


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


def resolve_db_url(explicit: str | None = None) -> str | None:
    """The Postgres DSN in play, if any: explicit arg, then ``IFTA_WEB_DB_URL``.

    Mirrors how ``ifta.web.db`` picks its backend, so a backup taken on a box
    always captures the same store the app is actually writing to.
    """
    if explicit is not None:
        return explicit or None
    return os.getenv("IFTA_WEB_DB_URL") or None


def resolve_keep(explicit: int | None = None) -> int:
    """Snapshots to retain: explicit arg, then ``$IFTA_BACKUP_KEEP``, then default."""
    if explicit is not None:
        return explicit
    raw = os.getenv("IFTA_BACKUP_KEEP")
    if not raw:
        return DEFAULT_KEEP
    try:
        return max(1, int(raw))
    except ValueError as exc:
        raise RuntimeError(f"IFTA_BACKUP_KEEP must be an integer, got {raw!r}") from exc


def split_dsn_password(db_url: str) -> tuple[str, str | None]:
    """Split a libpq URI into (password-free DSN, password).

    The password must not travel in ``argv``: it would be readable in
    ``/proc/<pid>/cmdline`` for the life of the dump, and ``TimeoutExpired``
    embeds the whole command in its message, so any traceback would print it.
    libpq reads ``PGPASSWORD`` from the environment instead.
    """
    parts = urlsplit(db_url)
    if "@" not in parts.netloc:
        return db_url, None
    userinfo, hostpart = parts.netloc.rsplit("@", 1)
    if ":" not in userinfo:
        return db_url, None
    user, raw_password = userinfo.split(":", 1)
    stripped = urlunsplit(
        (parts.scheme, f"{user}@{hostpart}", parts.path, parts.query, parts.fragment)
    )
    # The URI form is percent-encoded; PGPASSWORD wants the literal value.
    return stripped, unquote(raw_password)


def _dump_postgres(db_url: str, dest_file: Path) -> None:
    """Dump the job database with ``pg_dump`` in restorable custom format.

    Custom format (rather than plain SQL) so ``pg_restore`` can rebuild into an
    empty database selectively, and because it compresses on the way out.
    """
    safe_dsn, password = split_dsn_password(db_url)
    cmd = [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        f"--file={dest_file}",
        safe_dsn,
    ]
    env = dict(os.environ)
    if password is not None:
        env["PGPASSWORD"] = password
    try:
        # Fixed argv, no shell — and no credential in it either.
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_PG_DUMP_TIMEOUT_S,
            env=env,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "IFTA_WEB_DB_URL is set but pg_dump is not on PATH — the job database "
            "would be missing from the snapshot. Install a postgresql-client whose "
            "version is >= the server's."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"pg_dump timed out after {_PG_DUMP_TIMEOUT_S}s — is the database reachable?"
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(f"pg_dump failed ({proc.returncode}): {proc.stderr.strip()}")


def _stage(data_dir: Path, staging: Path, db_url: str | None = None) -> bool:
    """Copy ``data/`` into ``staging`` plus a consistent DB capture.

    Returns True when a Postgres dump was included (rather than SQLite).
    """
    for entry in sorted(data_dir.iterdir()):
        if entry.name in _SKIP:
            continue
        target = staging / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, ignore=shutil.ignore_patterns(".DS_Store"))
        else:
            shutil.copy2(entry, target)
    if db_url:
        # Job state lives in Postgres, not under data/ — capture it or the
        # restore silently comes back with zero submissions.
        _dump_postgres(db_url, staging / _PG_DUMP_NAME)
        return True
    db = data_dir / _DB_NAME
    if db.exists():
        _hot_copy_sqlite(db, staging / _DB_NAME)
    return False


def prune_snapshots(dest: Path, *, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` snapshots. Returns the ones removed."""
    snapshots = sorted(dest.glob(_SNAPSHOT_GLOB))
    stale = snapshots[:-keep] if keep > 0 else []
    for old in stale:
        old.unlink(missing_ok=True)
    return stale


def backup_data(
    project_root: Path,
    dest_dir: Path | None = None,
    *,
    keep: int = DEFAULT_KEEP,
    db_url: str | None = None,
) -> Path:
    """Snapshot ``project_root/data`` to a dated tar.gz and prune old ones."""
    return _backup_data_detailed(project_root, dest_dir, keep=keep, db_url=db_url)[0]


def _backup_data_detailed(
    project_root: Path,
    dest_dir: Path | None = None,
    *,
    keep: int = DEFAULT_KEEP,
    db_url: str | None = None,
) -> tuple[Path, bool]:
    """``backup_data`` plus whether a Postgres dump went into the archive."""
    data_dir = project_root / "data"
    if not data_dir.exists():
        raise FileNotFoundError(f"no data directory to back up: {data_dir}")
    dest = backup_dir(dest_dir)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"backup directory {dest} is not writable ({exc}). Is the backup volume "
            "mounted? Set IFTA_BACKUP_DIR to override."
        ) from exc

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    snapshot = dest / f"ifta-data-{stamp}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "data"
        staging.mkdir()
        included_pg = _stage(data_dir, staging, db_url=db_url)
        with tarfile.open(snapshot, "w:gz") as tar:
            tar.add(staging, arcname="data")

    prune_snapshots(dest, keep=keep)
    return snapshot, included_pg


def run_backup(
    project_root: Path,
    dest_dir: Path | None = None,
    *,
    keep: int | None = None,
    db_url: str | None = None,
    remote: RemoteConfig | None = None,
    replicate: bool = True,
) -> BackupResult:
    """Take a snapshot, replicate it to R2 when configured, prune both sides.

    This is the single entry point used by the CLI and the nightly timer. The
    local archive is written first and kept even if the upload fails — a
    snapshot on the box beats no snapshot at all — but the upload error still
    propagates so the timer reports failure instead of quietly drifting into a
    state where nothing has left the box for weeks.
    """
    keep = resolve_keep(keep)
    db_url = resolve_db_url(db_url)
    snapshot, included_pg = _backup_data_detailed(project_root, dest_dir, keep=keep, db_url=db_url)
    local_kept = len(list_snapshots(dest_dir))

    if remote is None and replicate:
        remote = RemoteConfig.from_env()
    if remote is None:
        return BackupResult(
            snapshot=snapshot,
            local_kept=local_kept,
            included_pg_dump=included_pg,
        )

    key = upload_snapshot(snapshot, remote)
    pruned = prune_remote(remote, keep=keep)
    return BackupResult(
        snapshot=snapshot,
        local_kept=local_kept,
        included_pg_dump=included_pg,
        remote_key=key,
        remote_kept=keep,
        remote_pruned=tuple(pruned),
    )


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

    On a Postgres deployment the extracted tree contains ``web_jobs.dump``,
    which is *not* restored here — feed it to ``pg_restore`` separately so the
    file swap and the database load stay independently verifiable
    (``docs/ORACLE.md``).
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
