"""Opt-in check against the real Gemini API:
    GEMINI_API_KEY=... pytest -m live
"""

import asyncio
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.llm import GeminiParser

pytestmark = pytest.mark.live
TZ = ZoneInfo("Europe/Sofia")
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=TZ)  # a Thursday


def parse(text):
    parser = GeminiParser(os.environ["GEMINI_API_KEY"], os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"))
    return asyncio.run(parser.parse(now=NOW, tz_name="Europe/Sofia", text=text))


def test_one_off():
    out = parse("remind me to call mom tomorrow evening")
    assert out.intent == "create"
    assert out.reminders[0].due_at == "2026-11-13T19:00"
    assert out.reminders[0].recurrence_rrule is None


def test_recurring_with_lead_time_and_importance():
    out = parse("pay rent on the 1st every month, remind me 3 days before, important")
    rem = out.reminders[0]
    assert rem.due_at.startswith("2026-12-01")
    assert "FREQ=MONTHLY" in rem.recurrence_rrule
    assert rem.remind_before_minutes == [4320]
    assert rem.importance in ("high", "urgent")


def test_query():
    out = parse("what do I have tomorrow?")
    assert out.intent == "query"
    assert out.query_from.startswith("2026-11-13")


def test_missing_time_asks():
    assert parse("remind me to buy milk").clarification_needed
