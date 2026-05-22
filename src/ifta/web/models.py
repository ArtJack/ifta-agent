"""Data models for the web intake layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SubmissionStatus(StrEnum):
    # Legacy email-confirm flow. New submissions use PENDING_APPROVAL instead.
    PENDING_CONFIRMATION = "pending_confirmation"
    # Awaiting operator accept/decline on Telegram. Intake brief already
    # generated; customer has been emailed the acknowledgement.
    PENDING_APPROVAL = "pending_approval"
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    # Operator declined on Telegram; customer was emailed the rejection.
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass
class Submission:
    id: str
    email: str
    quarter: str
    status: SubmissionStatus
    confirm_token: str
    created_at: datetime
    company: str | None = None
    error: str | None = None
    confirmed_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    packet_sent_at: datetime | None = None
    # Customer metadata from the new artjeck.com form (v2 /submit shape).
    name: str | None = None
    base_state: str | None = None
    fleet_size: int | None = None
    notes: str | None = None
    # Deterministic intake brief written before the operator decides.
    intake_brief_path: str | None = None
    intake_summary: str | None = None
    # Operator decision recorded when the Telegram button is tapped.
    decided_at: datetime | None = None
    decided_by: str | None = None
    decline_reason: str | None = None
    # Telegram approval card so we can edit it in place on decision.
    telegram_message_id: int | None = None
    telegram_chat_id: int | None = None
