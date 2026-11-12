"""SQLite storage. Datetimes are stored as UTC ISO strings."""

import json
import os
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from .models import Reminder

UTC = timezone.utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT NOT NULL,
    notes TEXT,
    importance TEXT NOT NULL,
    due_at TEXT,
    series_start TEXT,
    rrule TEXT,
    recurrence_text TEXT,
    remind_before TEXT NOT NULL,
    nag_every_min INTEGER,
    next_fire_at TEXT,
    status TEXT NOT NULL,
    last_fired_at TEXT,
    created_at TEXT,
    source_text TEXT
);
CREATE INDEX IF NOT EXISTS idx_due ON reminders (status, next_fire_at);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

FIELDS = [
    "task", "notes", "importance", "due_at", "series_start", "rrule",
    "recurrence_text", "remind_before", "nag_every_min", "next_fire_at",
    "status", "last_fired_at", "created_at", "source_text",
]
DATES = {"due_at", "series_start", "next_fire_at", "last_fired_at", "created_at"}


def _to_db(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(UTC).isoformat(timespec="seconds") if dt else None


def _from_db(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _values(rem: Reminder) -> list:
    out = []
    for name in FIELDS:
        value = getattr(rem, name)
        if name in DATES:
            value = _to_db(value)
        elif name == "remind_before":
            value = json.dumps(value)
        out.append(value)
    return out


def _row(row: aiosqlite.Row) -> Reminder:
    data = dict(row)
    for name in DATES:
        data[name] = _from_db(data[name])
    data["remind_before"] = json.loads(data["remind_before"])
    return Reminder(**data)


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def add(self, rem: Reminder) -> int:
        marks = ", ".join("?" for _ in FIELDS)
        cur = await self.conn.execute(
            f"INSERT INTO reminders ({', '.join(FIELDS)}) VALUES ({marks})", _values(rem)
        )
        await self.conn.commit()
        rem.id = cur.lastrowid
        return rem.id

    async def save(self, rem: Reminder) -> None:
        sets = ", ".join(f"{name} = ?" for name in FIELDS)
        await self.conn.execute(
            f"UPDATE reminders SET {sets} WHERE id = ?", _values(rem) + [rem.id]
        )
        await self.conn.commit()

    async def _fetch(self, where: str, params: tuple = ()) -> list[Reminder]:
        cur = await self.conn.execute(f"SELECT * FROM reminders WHERE {where}", params)
        return [_row(r) for r in await cur.fetchall()]

    async def get(self, reminder_id: int) -> Optional[Reminder]:
        found = await self._fetch("id = ?", (reminder_id,))
        return found[0] if found else None

    async def due(self, now: datetime) -> list[Reminder]:
        return await self._fetch(
            "status = 'active' AND next_fire_at IS NOT NULL AND next_fire_at <= ? "
            "ORDER BY next_fire_at",
            (_to_db(now),),
        )

    async def active(self) -> list[Reminder]:
        return await self._fetch("status = 'active' ORDER BY due_at IS NULL, due_at")

    async def get_setting(self, key: str) -> Optional[str]:
        cur = await self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()
