"""Data models for the web intake layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SubmissionStatus(StrEnum):
    PENDING_CONFIRMATION = "pending_confirmation"
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
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
