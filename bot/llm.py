"""Gemini client: turns a text or voice message into a ParsedIntent."""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from google import genai
from google.genai import errors, types

from .models import ParsedIntent, Reminder

log = logging.getLogger(__name__)

SYSTEM = """\
You are the parser of a personal reminder bot on Telegram. Convert the user's
message (text, or a voice recording) into JSON following the schema.

Current local date and time: {now} ({weekday}). Timezone: {tz}.
Every datetime you output is LOCAL wall-clock time formatted YYYY-MM-DDTHH:MM,
with no timezone offset.

Pick one intent:
- "create": the user wants to be reminded of something. Output one entry in
  `reminders` per distinct task.
- "query": the user asks what is scheduled. Set `query_from`/`query_to` to the
  range asked about ("tomorrow" = that whole day, "this week" = now until
  Sunday 23:59). Leave both null when no period is mentioned. Set
  `query_importance` only if they ask about one importance level.
- "complete": the user says a task is done. Put the matching ids from the
  list of active reminders in `target_ids`.
- "delete": the user wants to cancel/remove reminders. Put ids in `target_ids`.
- "chat": anything else. Put a short helpful answer in `reply`.

Rules for each reminder:
- `task`: what needs to be done, short, in the user's own language, without
  the time or the words "remind me".
- `due_at`: when it is due (first occurrence for a repeating task). It must be
  in the future. Resolve relative expressions against the current time.
  Vague times: morning 09:00, noon 12:00, afternoon 15:00, evening 19:00,
  night 21:00. When the user gives no time of day (e.g. "on the 1st every
  month", "every 2 weeks on Friday", "on 3 December"), use 09:00 on the next
  matching date in the future. Never copy the current time of day, except
  for expressions relative to now ("in 2 hours", "in 20 minutes").
- `recurrence_rrule`: an iCalendar RRULE without the "RRULE:" prefix and
  without DTSTART (e.g. "FREQ=WEEKLY;BYDAY=MO,TH"), or null for a one-off.
  An RRULE has a single unit, so convert a mixed interval to the smaller
  unit and never drop part of it: "every 12 days and 2 hours" ->
  "FREQ=HOURLY;INTERVAL=290"; "every hour and a half" ->
  "FREQ=MINUTELY;INTERVAL=90".
  `recurrence_text`: the same in a few plain words in the user's language
  (e.g. "every Monday and Thursday"), or null.
- `remind_before_minutes`: ONLY lead times the user explicitly asked for
  ("remind me 3 days before" -> [4320]; "a day and an hour before" ->
  [1440, 60]). Otherwise an empty list.
- `nag_every_minutes`: ONLY if the user asks to keep being reminded until it
  is done ("keep bugging me every 10 minutes" -> 10). Otherwise null.
- `importance`: low, medium, high or urgent, inferred from the wording
  ("important", "don't let me forget", "critical", "whenever", ...).
  Default medium.
- `notes`: extra details that don't belong in the task, or null.

If you cannot tell WHEN a new reminder is due, or which existing reminder is
meant, do not guess: put one short question in `clarification_needed` (in the
user's language). Otherwise it is null.

For a voice recording, put the verbatim transcript in `transcript`; for text
it is null. Unused fields are null or empty lists.
"""


class LLMError(Exception):
    pass


class GeminiParser:
    def __init__(self, api_key: str, model: str):
        self.client = genai.Client(api_key=api_key)
        self.model = model

    async def parse(
        self,
        *,
        now: datetime,
        tz_name: str,
        text: Optional[str] = None,
        audio: Optional[bytes] = None,
        audio_mime: str = "audio/ogg",
        active: Optional[list[Reminder]] = None,
        prior: Optional[str] = None,
    ) -> ParsedIntent:
        """`now` is the current time in the user's timezone."""
        system = SYSTEM.format(
            now=now.strftime("%Y-%m-%dT%H:%M"), weekday=now.strftime("%A"), tz=tz_name
        )
        parts: list[types.Part] = []
        lines = ["Active reminders (id | due | task):"]
        for rem in active or []:
            due = rem.due_at.astimezone(now.tzinfo).strftime("%Y-%m-%dT%H:%M") if rem.due_at else "no date"
            lines.append(f"{rem.id} | {due} | {rem.task}")
        if len(lines) == 1:
            lines.append("(none)")
        parts.append(types.Part.from_text(text="\n".join(lines)))
        if prior:
            parts.append(types.Part.from_text(text="Context from the conversation so far:\n" + prior))
        if audio is not None:
            parts.append(types.Part.from_text(text="The user's message is this voice recording:"))
            parts.append(types.Part.from_bytes(data=audio, mime_type=audio_mime))
        else:
            parts.append(types.Part.from_text(text="The user's message:\n" + (text or "")))

        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=ParsedIntent,
            temperature=0,
        )

        last: Optional[Exception] = None
        for attempt in range(3):
            try:
                response = await self.client.aio.models.generate_content(
                    model=self.model,
                    contents=[types.Content(role="user", parts=parts)],
                    config=config,
                )
                if isinstance(response.parsed, ParsedIntent):
                    return response.parsed
                return ParsedIntent.model_validate_json(response.text or "")
            except errors.APIError as exc:
                last = exc
                if exc.code not in (429, 500, 503):
                    break
                log.warning("Gemini returned %s, retrying", exc.code)
                await asyncio.sleep(2 * 3**attempt)
            except ValueError as exc:  # malformed or empty JSON
                last = exc
                break
        raise LLMError(str(last)) from last
