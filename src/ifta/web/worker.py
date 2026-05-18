"""Polling worker that drains the QUEUED submissions table.

Separate process from the FastAPI app — run via `ifta worker`. Designed so
Phase 3 can hook in email-sending via the `on_success` / `on_failure`
callbacks without coupling the worker to a specific email provider.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from ifta.web import db
from ifta.web.models import Submission
from ifta.web.pipeline import PipelineError, process_submission

log = logging.getLogger("ifta.web.worker")

SuccessCallback = Callable[[Submission, Path], None]
FailureCallback = Callable[[Submission, str], None]


def process_one_job(
    db_path: Path,
    submissions_dir: Path,
    *,
    on_success: SuccessCallback | None = None,
    on_failure: FailureCallback | None = None,
) -> Submission | None:
    """Claim and process one QUEUED submission.

    Returns the submission (in its final DONE/FAILED state) or None if the
    queue is empty. Exceptions from `process_submission` are caught and
    surfaced via `on_failure` — the worker loop must not crash on bad input.
    """
    sub = db.claim_next_queued(db_path)
    if sub is None:
        return None

    log.info("processing submission %s (quarter=%s)", sub.id, sub.quarter)
    try:
        out_dir = process_submission(submissions_dir, sub)
    except PipelineError as e:
        # Customer-actionable error (bad files, preflight, missing data).
        db.mark_failed(db_path, sub.id, error=str(e))
        log.warning("submission %s failed (PipelineError): %s", sub.id, e)
        if on_failure:
            try:
                on_failure(sub, str(e))
            except Exception:
                log.exception("on_failure callback raised for %s", sub.id)
        return db.get_submission(db_path, sub.id)
    except Exception as e:
        # Unexpected — log full trace, surface short message to customer.
        log.exception("submission %s failed (unexpected): %s", sub.id, e)
        db.mark_failed(
            db_path,
            sub.id,
            error=f"Internal error while processing submission: {e}",
        )
        if on_failure:
            try:
                on_failure(sub, str(e))
            except Exception:
                log.exception("on_failure callback raised for %s", sub.id)
        return db.get_submission(db_path, sub.id)

    db.mark_done(db_path, sub.id)
    log.info("submission %s done — outputs at %s", sub.id, out_dir)
    if on_success:
        try:
            on_success(sub, out_dir)
        except Exception:
            log.exception("on_success callback raised for %s", sub.id)
    return db.get_submission(db_path, sub.id)


def run_forever(
    db_path: Path,
    submissions_dir: Path,
    *,
    poll_interval_seconds: float = 5.0,
    on_success: SuccessCallback | None = None,
    on_failure: FailureCallback | None = None,
) -> None:
    """Block forever, draining the queue. Stop with Ctrl-C."""
    log.info(
        "worker starting — db=%s submissions=%s poll=%.1fs",
        db_path,
        submissions_dir,
        poll_interval_seconds,
    )
    while True:
        try:
            sub = process_one_job(
                db_path,
                submissions_dir,
                on_success=on_success,
                on_failure=on_failure,
            )
        except KeyboardInterrupt:
            log.info("worker stopping (Ctrl-C)")
            return
        if sub is None:
            time.sleep(poll_interval_seconds)
