"""Pure scheduling logic: when a reminder fires next. No I/O, easy to test."""

import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional

from dateutil.rrule import rrulestr

from .models import Reminder

UTC = timezone.utc


def clean_rrule(rule: Optional[str]) -> Optional[str]:
    """Normalise an RRULE string; return None if it is missing or invalid."""
    if not rule:
        return None
    rule = rule.strip().upper()
    if rule.startswith("RRULE:"):
        rule = rule[6:]
    # Occurrences are computed in local wall-clock time, so UNTIL must be naive.
    rule = re.sub(r"(UNTIL=\d{8}(T\d{6})?)Z", r"\1", rule)
    try:
        rrulestr(rule, dtstart=datetime(2000, 1, 1))
    except (ValueError, TypeError):
        return None
    return rule


def _local(dt: datetime, tz: tzinfo) -> datetime:
    return dt.astimezone(tz).replace(tzinfo=None)


def _rule(rem: Reminder, tz: tzinfo):
    start = rem.series_start or rem.due_at
    return rrulestr(rem.rrule, dtstart=_local(start, tz))


def next_occurrence(rem: Reminder, after: datetime, tz: tzinfo) -> Optional[datetime]:
    """Next occurrence of a recurring reminder strictly after `after`."""
    nxt = _rule(rem, tz).after(_local(after, tz), inc=False)
    return nxt.replace(tzinfo=tz).astimezone(UTC) if nxt else None


def occurrences(
    rem: Reminder, start: datetime, end: datetime, tz: tzinfo, limit: int = 60
) -> list[datetime]:
    """Occurrences of a reminder within [start, end], from its current one on."""
    if rem.due_at is None:
        return []
    if not rem.rrule:
        return [rem.due_at] if start <= rem.due_at <= end else []
    lo = max(start, rem.due_at)
    if lo > end:
        return []
    found = _rule(rem, tz).between(_local(lo, tz), _local(end, tz), inc=True)
    return [d.replace(tzinfo=tz).astimezone(UTC) for d in found[:limit]]


def _points(rem: Reminder) -> list[datetime]:
    offsets = set(rem.remind_before) | {0}
    return sorted(rem.due_at - timedelta(minutes=m) for m in offsets)


def _advance(rem: Reminder, now: datetime, tz: tzinfo) -> None:
    """Move a recurring reminder to its next occurrence, or finish it."""
    nxt = next_occurrence(rem, max(now, rem.due_at), tz)
    if nxt is None:
        rem.status = "done"
        rem.next_fire_at = None
        return
    rem.due_at = nxt
    rem.next_fire_at = min(p for p in _points(rem) if p > now)


def reschedule(rem: Reminder, now: datetime, tz: tzinfo) -> None:
    """Set next_fire_at (and due_at/status) after creation or after a firing.

    The caller sets rem.last_fired_at = now before calling this after a firing.
    """
    if rem.status != "active" or rem.due_at is None:
        rem.next_fire_at = None
        return

    anchor = rem.last_fired_at or rem.created_at or now
    pending = [p for p in _points(rem) if p > anchor]
    if pending:
        rem.next_fire_at = pending[0]
        return

    # Every reminder point of this occurrence has passed.
    if rem.last_fired_at is None:
        # Created with a due time already in the past.
        if rem.rrule:
            _advance(rem, now, tz)
        else:
            rem.next_fire_at = now
        return

    if rem.nag_every_min:
        rem.next_fire_at = rem.last_fired_at + timedelta(minutes=rem.nag_every_min)
    elif rem.rrule:
        _advance(rem, now, tz)
    else:
        rem.status = "done"
        rem.next_fire_at = None


def mark_done(rem: Reminder, now: datetime, tz: tzinfo) -> str:
    """Complete a reminder. Returns 'done', 'next' (moved to the next
    occurrence), 'not_due' (recurring, nothing to complete yet) or 'already'."""
    if rem.status != "active":
        return "already"
    if not rem.rrule or rem.due_at is None:
        rem.status = "done"
        rem.next_fire_at = None
        return "done"

    # Recurring. Completing only makes sense if this occurrence is overdue or
    # one of its reminders has already been sent; otherwise it is an upcoming
    # occurrence the scheduler has already moved to.
    window_start = rem.due_at - timedelta(minutes=max(rem.remind_before, default=0))
    started = rem.last_fired_at is not None and rem.last_fired_at >= window_start
    if rem.due_at > now and not started:
        return "not_due"
    rem.last_fired_at = now
    _advance(rem, now, tz)
    return "done" if rem.status == "done" else "next"


def snooze(rem: Reminder, until: datetime) -> None:
    rem.status = "active"
    rem.next_fire_at = until
