"""Data models: the JSON schema Gemini fills in, and the stored reminder."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel

Importance = Literal["low", "medium", "high", "urgent"]

# Used when the user doesn't say how they want to be reminded:
# importance -> (minutes before the due time, repeat-until-done interval)
TIMING_DEFAULTS: dict[str, tuple[list[int], Optional[int]]] = {
    "low": ([0], None),
    "medium": ([0], None),
    "high": ([1440, 60, 0], 60),
    "urgent": ([0], 15),
}


# --- Gemini output schema. Every field is required (nullable where optional),
# --- because default values are not reliably supported in response schemas.


class ReminderDraft(BaseModel):
    task: str
    due_at: Optional[str]
    recurrence_rrule: Optional[str]
    recurrence_text: Optional[str]
    remind_before_minutes: list[int]
    nag_every_minutes: Optional[int]
    importance: Importance
    notes: Optional[str]


class ParsedIntent(BaseModel):
    intent: Literal["create", "query", "complete", "delete", "chat"]
    transcript: Optional[str]
    reminders: list[ReminderDraft]
    query_from: Optional[str]
    query_to: Optional[str]
    query_importance: Optional[Importance]
    target_ids: list[int]
    clarification_needed: Optional[str]
    reply: Optional[str]


# --- Stored reminder. All datetimes are timezone-aware UTC.


@dataclass
class Reminder:
    task: str
    importance: str = "medium"
    due_at: Optional[datetime] = None  # current occurrence
    series_start: Optional[datetime] = None  # first occurrence (RRULE anchor)
    rrule: Optional[str] = None
    recurrence_text: Optional[str] = None
    remind_before: list[int] = field(default_factory=lambda: [0])
    nag_every_min: Optional[int] = None
    next_fire_at: Optional[datetime] = None
    status: str = "active"  # active | done | cancelled
    last_fired_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    notes: Optional[str] = None
    source_text: Optional[str] = None
    id: Optional[int] = None
