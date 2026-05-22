"""Reserve copies of customer-submitted raw files.

After a submission is processed, its raw inbox files are copied into a dated
archive so the originals survive even if the live submissions directory is
cleaned up or a submission is re-run. Pure filesystem — no network, never
raises on individual file errors (best-effort archival must not break delivery).
"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("ifta.backup")


def default_archive_root(submissions_dir: Path) -> Path:
    """Sibling `backups/` dir next to the live submissions directory."""
    return submissions_dir.parent / "backups"


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "submission"


def archive_inputs(
    inbox: Path,
    *,
    archive_root: Path,
    quarter: str,
    company: str | None,
    submission_id: str,
) -> list[Path]:
    """Copy every file in `inbox` into a dated archive folder.

    Destination: `<archive_root>/<quarter>/<YYYYMMDD-HHMMSS>_<company>_<sid8>/`.
    Returns the list of archived file paths (empty if the inbox has no files or
    doesn't exist). Per-file copy errors are logged and skipped.
    """
    if not inbox.exists():
        return []
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    label = _slug(company or "unknown")
    dest = archive_root / quarter / f"{stamp}_{label}_{submission_id[:8]}"
    copied: list[Path] = []
    for src in sorted(inbox.iterdir()):
        if not src.is_file():
            continue
        try:
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / src.name
            shutil.copy2(src, target)
            copied.append(target)
        except OSError as e:
            log.warning("failed to archive %s: %s", src, e)
    return copied
