"""Job-state persistence facade — dispatches to SQLite or Postgres.

The web intake stores submission state in SQLite on the single-host (Mac mini)
deployment and in PostgreSQL on Azure. The backend is chosen by environment:

    IFTA_WEB_DB_URL set  ->  PostgreSQL  (ifta.web.db_postgres)
    unset                ->  SQLite      (ifta.web.db_sqlite)

Both backends expose an identical function surface, so callers keep doing
``from ifta.web import db; db.create_submission(path, ...)`` unchanged. The
first ``path`` argument is used by SQLite and ignored by Postgres (which reads
its DSN from IFTA_WEB_DB_URL). psycopg is imported lazily — only when a URL is
set — so deployments without the ``[azure]`` extra never require it.
"""

from __future__ import annotations

import os
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Expose the shared backend contract to type-checkers (both backends match
    # these signatures). At runtime these names resolve via __getattr__ below.
    from ifta.web.db_sqlite import (  # noqa: F401
        approve_submission,
        claim_next_queued,
        confirm_submission,
        create_submission,
        get_submission,
        get_submission_by_token,
        init_db,
        list_submissions,
        mark_done,
        mark_failed,
        mark_packet_sent,
        reap_stale_running,
        reject_submission,
        reopen_for_review,
        request_more_files_submission,
        update_intake_brief,
        update_telegram_card,
    )


def _use_postgres() -> bool:
    return bool(os.environ.get("IFTA_WEB_DB_URL"))


def _backend() -> ModuleType:
    """Return the active backend module, resolved per call from the environment."""
    if _use_postgres():
        from ifta.web import db_postgres

        return db_postgres
    from ifta.web import db_sqlite

    return db_sqlite


def __getattr__(name: str) -> object:
    # PEP 562: forward every public ``db.*`` access to the selected backend.
    return getattr(_backend(), name)
