import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot import scheduler
from bot.db import Database
from bot.handlers import build_reminder
from bot.models import Reminder, ReminderDraft

UTC = timezone.utc
TZ = ZoneInfo("Europe/Sofia")


def local(*args) -> datetime:
    return datetime(*args, tzinfo=TZ).astimezone(UTC)


def make(now, due, **kw) -> Reminder:
    rem = Reminder(task="t", due_at=due, series_start=due, created_at=now, **kw)
    scheduler.reschedule(rem, now, TZ)
    return rem


def fire(rem, now):
    rem.last_fired_at = now
    scheduler.reschedule(rem, now, TZ)


def test_one_off_fires_once_then_done():
    now = local(2026, 11, 12, 10, 0)
    due = local(2026, 11, 12, 12, 0)
    rem = make(now, due)
    assert rem.next_fire_at == due
    fire(rem, due)
    assert rem.status == "done" and rem.next_fire_at is None


def test_lead_times_in_order_and_past_ones_skipped():
    now = local(2026, 11, 12, 10, 0)
    due = local(2026, 11, 12, 20, 0)  # 1 day before is already past
    rem = make(now, due, remind_before=[1440, 60, 0])
    assert rem.next_fire_at == due - timedelta(hours=1)
    fire(rem, rem.next_fire_at)
    assert rem.next_fire_at == due


def test_nag_until_done():
    now = local(2026, 11, 12, 10, 0)
    due = local(2026, 11, 12, 11, 0)
    rem = make(now, due, nag_every_min=15)
    fire(rem, due)
    assert rem.status == "active"
    assert rem.next_fire_at == due + timedelta(minutes=15)
    fire(rem, rem.next_fire_at)
    assert rem.next_fire_at == due + timedelta(minutes=30)
    assert scheduler.mark_done(rem, due + timedelta(minutes=31), TZ) == "done"
    assert rem.status == "done" and rem.next_fire_at is None


def test_weekly_advances_and_keeps_wall_clock_across_dst():
    # Mondays 09:00 local; DST starts in Sofia on 29 March 2026.
    now = local(2026, 3, 20, 10, 0)
    due = local(2026, 3, 23, 9, 0)
    rem = make(now, due, rrule="FREQ=WEEKLY;BYDAY=MO")
    fire(rem, due)
    assert rem.status == "active"
    assert rem.due_at == local(2026, 3, 30, 9, 0)
    assert rem.due_at.astimezone(TZ).hour == 9
    assert rem.next_fire_at == rem.due_at


def test_interval_and_count_use_series_start():
    now = local(2026, 11, 1, 10, 0)
    due = local(2026, 11, 2, 9, 0)
    rem = make(now, due, rrule="FREQ=DAILY;INTERVAL=2;COUNT=3")
    fire(rem, due)
    assert rem.due_at == local(2026, 11, 4, 9, 0)
    fire(rem, rem.due_at)
    assert rem.due_at == local(2026, 11, 6, 9, 0)
    fire(rem, rem.due_at)
    assert rem.status == "done"


def test_missed_occurrences_while_offline_fire_once():
    now = local(2026, 11, 1, 10, 0)
    due = local(2026, 11, 2, 9, 0)
    rem = make(now, due, rrule="FREQ=DAILY")
    later = local(2026, 11, 5, 12, 0)  # bot was down for three days
    fire(rem, later)
    assert rem.due_at == local(2026, 11, 6, 9, 0)


def test_recurring_done_early_skips_to_next():
    now = local(2026, 11, 1, 10, 0)
    due = local(2026, 11, 10, 9, 0)
    rem = make(now, due, rrule="FREQ=MONTHLY", remind_before=[1440, 0], nag_every_min=60)
    fire(rem, due - timedelta(days=1))  # the "1 day before" reminder
    assert scheduler.mark_done(rem, due - timedelta(hours=20), TZ) == "next"
    assert rem.due_at == local(2026, 12, 10, 9, 0)
    assert rem.next_fire_at == rem.due_at - timedelta(days=1)


def test_done_on_already_advanced_recurring_does_not_skip():
    now = local(2026, 11, 1, 10, 0)
    due = local(2026, 11, 2, 9, 0)
    rem = make(now, due, rrule="FREQ=DAILY")
    fire(rem, due)
    nxt = rem.due_at
    assert scheduler.mark_done(rem, due + timedelta(minutes=5), TZ) == "not_due"
    assert rem.due_at == nxt


def test_snooze_reactivates_and_refires():
    now = local(2026, 11, 12, 10, 0)
    due = local(2026, 11, 12, 11, 0)
    rem = make(now, due)
    fire(rem, due)
    assert rem.status == "done"
    until = due + timedelta(minutes=10)
    scheduler.snooze(rem, until)
    assert rem.status == "active" and rem.next_fire_at == until
    fire(rem, until)
    assert rem.status == "done"


def test_past_due_on_creation():
    now = local(2026, 11, 12, 10, 0)
    assert make(now, local(2026, 11, 12, 9, 0)).next_fire_at == now
    weekly = make(now, local(2026, 11, 5, 9, 0), rrule="FREQ=WEEKLY")
    assert weekly.due_at == local(2026, 11, 19, 9, 0)


def test_clean_rrule():
    assert scheduler.clean_rrule("rrule:freq=daily") == "FREQ=DAILY"
    assert scheduler.clean_rrule("FREQ=DAILY;UNTIL=20261231T000000Z") == "FREQ=DAILY;UNTIL=20261231T000000"
    assert scheduler.clean_rrule("every day") is None
    assert scheduler.clean_rrule(None) is None


def test_occurrences_in_range():
    now = local(2026, 11, 12, 10, 0)
    rem = make(now, local(2026, 11, 13, 9, 0), rrule="FREQ=DAILY")
    got = scheduler.occurrences(rem, now, now + timedelta(days=3), TZ)
    assert got == [local(2026, 11, 13, 9, 0), local(2026, 11, 14, 9, 0), local(2026, 11, 15, 9, 0)]


def test_build_reminder_applies_importance_defaults():
    now = local(2026, 11, 12, 10, 0)

    def draft(**kw):
        base = dict(
            task="x", due_at="2026-11-20T09:00", recurrence_rrule=None, recurrence_text=None,
            remind_before_minutes=[], nag_every_minutes=None, importance="medium", notes=None,
        )
        return ReminderDraft(**{**base, **kw})

    high = build_reminder(draft(importance="high"), TZ, now, None)
    assert high.remind_before == [1440, 60, 0] and high.nag_every_min == 60
    assert high.due_at == local(2026, 11, 20, 9, 0)
    explicit = build_reminder(draft(importance="high", remind_before_minutes=[4320]), TZ, now, None)
    assert explicit.remind_before == [4320, 0] and explicit.nag_every_min is None
    bad = build_reminder(draft(recurrence_rrule="nonsense"), TZ, now, None)
    assert bad.rrule is None


def test_db_roundtrip_and_due(tmp_path):
    async def run():
        db = Database(str(tmp_path / "sub" / "r.db"))
        await db.connect()
        try:
            now = local(2026, 11, 12, 10, 0)
            rem = make(now, local(2026, 11, 12, 11, 0), remind_before=[60, 0], nag_every_min=5)
            await db.add(rem)
            loaded = await db.get(rem.id)
            assert loaded == rem
            assert await db.due(now - timedelta(minutes=1)) == []
            assert [r.id for r in await db.due(now + timedelta(hours=2))] == [rem.id]
            loaded.status = "cancelled"
            await db.save(loaded)
            assert await db.active() == []
            await db.set_setting("tz", "Europe/Sofia")
            assert await db.get_setting("tz") == "Europe/Sofia"
        finally:
            await db.close()

    asyncio.run(run())
