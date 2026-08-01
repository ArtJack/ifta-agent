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
from ifta.web.models import Submission, SubmissionStatus
from ifta.web.pipeline import PipelineError, process_submission

log = logging.getLogger("ifta.web.worker")

SuccessCallback = Callable[[Submission, Path], None]
FailureCallback = Callable[[Submission, str], None]

# How many times a submission may be claimed before an unexpected failure is
# treated as permanent. Transient causes (iftach.org down, model API blip, a
# Postgres failover) clear on their own; telling a customer to re-upload for
# one of those loses them for a reason that had nothing to do with their files.
DEFAULT_MAX_ATTEMPTS = 3


def process_one_job(
    db_path: Path,
    submissions_dir: Path,
    *,
    on_success: SuccessCallback | None = None,
    on_failure: FailureCallback | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Submission | None:
    """Claim and process one QUEUED submission.

    Returns the submission (in its final DONE/FAILED state) or None if the
    queue is empty. Exceptions from `process_submission` are caught and
    surfaced via `on_failure` — the worker loop must not crash on bad input.

    Two kinds of failure are treated differently:

    * `PipelineError` is the customer's to fix (unreadable files, nothing
      parsable, preflight errors). It fails immediately and emails them.
    * Anything else is assumed transient and the job goes back on the queue,
      up to `max_attempts` claims. The customer hears nothing until the
      retries are exhausted, because there is nothing for them to do.
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
        # Unexpected — assume transient and retry before giving up on it.
        log.exception(
            "submission %s failed (unexpected, attempt %d/%d): %s",
            sub.id,
            sub.attempts,
            max_attempts,
            e,
        )
        if sub.attempts < max_attempts:
            requeued = db.requeue_for_retry(
                db_path,
                sub.id,
                error=f"Attempt {sub.attempts} failed, will retry: {e}",
            )
            if requeued is not None:
                log.info(
                    "submission %s requeued for retry (%d/%d)",
                    sub.id,
                    sub.attempts,
                    max_attempts,
                )
                return requeued
            # Couldn't requeue (someone else moved the row) — fall through and
            # record the failure rather than silently dropping the job.
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
    stale_running_timeout_seconds: int = 900,
    on_success: SuccessCallback | None = None,
    on_failure: FailureCallback | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> None:
    """Block forever, draining the queue. Stop with Ctrl-C."""
    log.info(
        "worker starting — db=%s submissions=%s poll=%.1fs",
        db.describe_backend(db_path),
        submissions_dir,
        poll_interval_seconds,
    )

    def reap(reason: str) -> None:
        """Recover submissions orphaned in RUNNING by a dead worker.

        A crash mid-job is not the customer's fault, so an orphan with attempts
        left goes back on the queue instead of emailing them to re-upload.
        Only once the retries are spent does it become a real failure.
        """
        for reaped_sub in db.reap_stale_running(
            db_path, max_seconds_running=stale_running_timeout_seconds
        ):
            if reaped_sub.attempts < max_attempts:
                requeued = db.requeue_for_retry(
                    db_path,
                    reaped_sub.id,
                    error=(
                        f"Worker stopped mid-job on attempt {reaped_sub.attempts}; "
                        "retrying automatically."
                    ),
                    from_status=SubmissionStatus.FAILED,
                )
                if requeued is not None:
                    log.warning(
                        "reaped stale submission %s (%s) — requeued (%d/%d)",
                        reaped_sub.id,
                        reason,
                        reaped_sub.attempts,
                        max_attempts,
                    )
                    continue
            log.warning(
                "reaped stale submission %s (%s) — marking failed after %d attempt(s)",
                reaped_sub.id,
                reason,
                reaped_sub.attempts,
            )
            if on_failure:
                try:
                    on_failure(reaped_sub, reaped_sub.error or "worker crashed mid-job")
                except Exception:
                    log.exception(
                        "on_failure callback raised for reaped %s", reaped_sub.id
                    )

    # Recover submissions left RUNNING by a previous crashed worker. Failure
    # emails fire here so customers learn their submission didn't complete
    # instead of waiting forever.
    reap("startup")

    while True:
        try:
            sub = process_one_job(
                db_path,
                submissions_dir,
                on_success=on_success,
                on_failure=on_failure,
                max_attempts=max_attempts,
            )
            if sub is None:
                # Reap on the idle path too. A startup-only reap can never
                # recover the common crash: launchd KeepAlive / ACA restart the
                # worker within seconds, so the orphan is far younger than the
                # stale cutoff at startup and nothing checks it again — the
                # customer's job would sit in RUNNING forever.
                reap("idle sweep")
                # Sleep inside the try so Ctrl-C during the idle wait exits
                # cleanly instead of dumping a stack trace to the operator.
                time.sleep(poll_interval_seconds)
        except KeyboardInterrupt:
            log.info("worker stopping (Ctrl-C)")
            return
        except Exception:
            # A transient DB error (Postgres failover, idle disconnect, Azure
            # Burstable maintenance) must not kill the worker — that is exactly
            # what manufactures an orphaned RUNNING row. Log, back off, retry.
            log.exception("worker loop error — retrying in %.1fs", poll_interval_seconds)
            try:
                time.sleep(poll_interval_seconds)
            except KeyboardInterrupt:
                log.info("worker stopping (Ctrl-C)")
                return
