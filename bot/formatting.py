"""Message texts (Telegram HTML)."""

from datetime import datetime, timedelta, timezone, tzinfo
from html import escape

from . import scheduler
from .models import Reminder

ICON = {"low": "🔵", "medium": "🟡", "high": "🟠", "urgent": "🔴"}


def duration(minutes: int) -> str:
    minutes = max(int(minutes), 0)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} h")
    if mins or not parts:
        parts.append(f"{mins} min")
    return " ".join(parts)


def when(dt: datetime, tz: tzinfo) -> str:
    return dt.astimezone(tz).strftime("%a %d %b %Y, %H:%M")


def timing(rem: Reminder) -> str:
    offsets = sorted(set(rem.remind_before) | {0}, reverse=True)
    parts = [f"{duration(m)} before" if m else "at the due time" for m in offsets]
    text = ", ".join(parts)
    if rem.nag_every_min:
        text += f"; then every {duration(rem.nag_every_min)} until done"
    return text


def card(rem: Reminder, tz: tzinfo) -> str:
    lines = [f"📌 <b>{escape(rem.task)}</b>"]
    if rem.due_at:
        lines.append(f"🗓 {when(rem.due_at, tz)}")
        if rem.rrule:
            lines.append(f"🔁 {escape(rem.recurrence_text or rem.rrule)}")
            # Computed from the actual rule, so a misparsed pattern is visible.
            dates = scheduler.upcoming(rem, datetime.now(timezone.utc), tz)
            if dates:
                shown = " · ".join(d.astimezone(tz).strftime("%a %d %b %H:%M") for d in dates)
                lines.append(f"     <i>Next: {shown}</i>")
        lines.append(f"⏰ {timing(rem)}")
    else:
        lines.append("🗓 No date — it will only show up in /list")
    lines.append(f"{ICON[rem.importance]} {rem.importance.capitalize()}")
    if rem.notes:
        lines.append(f"📝 {escape(rem.notes)}")
    return "\n".join(lines)


def notification(rem: Reminder, now: datetime, tz: tzinfo, missed: bool) -> str:
    lines = [f"⏰ {ICON[rem.importance]} <b>{escape(rem.task)}</b>"]
    if rem.due_at:
        delta = int((rem.due_at - now).total_seconds() // 60)
        if delta > 1:
            rel = f"in {duration(delta)}"
        elif delta < -1:
            rel = f"overdue by {duration(-delta)}"
        else:
            rel = "now"
        lines.append(f"🗓 {when(rem.due_at, tz)} ({rel})")
    if rem.notes:
        lines.append(f"📝 {escape(rem.notes)}")
    if missed:
        lines.append("<i>Sent late — the bot was offline when this was due.</i>")
    return "\n".join(lines)


def listing(
    title: str,
    items: list[tuple[datetime, Reminder]],
    tz: tzinfo,
    now: datetime,
    undated: list[Reminder] = (),
) -> str:
    """`items` are (occurrence time, reminder) pairs."""
    if not items and not undated:
        return f"<b>{escape(title)}</b>\nNothing scheduled."
    lines = [f"<b>{escape(title)}</b>"]
    day = None
    for dt, rem in sorted(items, key=lambda pair: pair[0]):
        local = dt.astimezone(tz)
        if dt < now:
            heading = "Overdue"
        else:
            heading = local.strftime("%a %d %b")
        if heading != day:
            day = heading
            lines.append(f"\n<u>{heading}</u>")
        stamp = local.strftime("%d %b %H:%M") if dt < now else local.strftime("%H:%M")
        repeat = " 🔁" if rem.rrule else ""
        lines.append(f"{stamp} {ICON[rem.importance]} {escape(rem.task)}{repeat} <code>#{rem.id}</code>")
    if undated:
        lines.append("\n<u>No date</u>")
        for rem in undated:
            lines.append(f"{ICON[rem.importance]} {escape(rem.task)} <code>#{rem.id}</code>")
    return "\n".join(lines)


def tomorrow_morning(now_local: datetime) -> datetime:
    return (now_local + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
