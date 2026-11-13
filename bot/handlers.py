"""Telegram handlers: messages, commands, buttons and the scheduler tick."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import formatting as fmt
from . import scheduler
from .config import Settings
from .db import Database
from .llm import GeminiParser, LLMError
from .models import TIMING_DEFAULTS, ParsedIntent, Reminder, ReminderDraft

log = logging.getLogger(__name__)
UTC = timezone.utc
HTML = ParseMode.HTML

HELP = """\
Send me a text or voice message describing what to remind you about, e.g.
<i>"pay rent on the 1st every month, remind me 3 days before, important"</i>

Ask in plain words what is coming up (<i>"what do I have this week?"</i>), or use:
/list — everything upcoming
/today — today
/week — next 7 days
/done &lt;id&gt; — mark as done
/delete &lt;id&gt; — cancel a reminder
/tz &lt;Area/City&gt; — set your timezone"""


@dataclass
class State:
    settings: Settings
    db: Database
    llm: GeminiParser
    tz: ZoneInfo
    drafts: dict[int, Reminder] = field(default_factory=dict)
    draft_seq: int = 0


def _state(context: ContextTypes.DEFAULT_TYPE) -> State:
    return context.application.bot_data["state"]


def _now() -> datetime:
    return datetime.now(UTC)


# --- building reminders from parsed drafts -------------------------------


def _parse_local(value: Optional[str], tz: ZoneInfo) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC)


def build_reminder(draft: ReminderDraft, tz: ZoneInfo, now: datetime, source: Optional[str]) -> Reminder:
    due = _parse_local(draft.due_at, tz)
    before = sorted({m for m in draft.remind_before_minutes if 0 < m <= 525600})
    nag = draft.nag_every_minutes if draft.nag_every_minutes and draft.nag_every_minutes > 0 else None
    if not before and nag is None:
        default_before, nag = TIMING_DEFAULTS[draft.importance]
        before = list(default_before)
    rule = scheduler.clean_rrule(draft.recurrence_rrule) if due else None
    rem = Reminder(
        task=draft.task.strip() or "Reminder",
        importance=draft.importance,
        due_at=due,
        series_start=due,
        rrule=rule,
        recurrence_text=draft.recurrence_text if rule else None,
        remind_before=sorted(set(before) | {0}, reverse=True),
        nag_every_min=nag,
        notes=draft.notes,
        source_text=source,
        created_at=now,
    )
    if rule and due < now:
        # First occurrence already passed (e.g. "every Friday" said on a Friday
        # afternoon): show and schedule the next one. series_start keeps the
        # original anchor so intervals stay aligned.
        rem.due_at = scheduler.next_occurrence(rem, now, tz) or due
    return rem


def _draft_json(rem: Reminder, tz: ZoneInfo) -> str:
    return json.dumps(
        {
            "task": rem.task,
            "due_at": rem.due_at.astimezone(tz).strftime("%Y-%m-%dT%H:%M") if rem.due_at else None,
            "recurrence_rrule": rem.rrule,
            "recurrence_text": rem.recurrence_text,
            "remind_before_minutes": [m for m in rem.remind_before if m],
            "nag_every_minutes": rem.nag_every_min,
            "importance": rem.importance,
            "notes": rem.notes,
        },
        ensure_ascii=False,
    )


# --- keyboards ------------------------------------------------------------


def _draft_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Save", callback_data=f"s:{draft_id}"),
            InlineKeyboardButton("✏️ Edit", callback_data=f"e:{draft_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"c:{draft_id}"),
        ]]
    )


def _fired_keyboard(rem_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✔️ Done", callback_data=f"d:{rem_id}"),
            InlineKeyboardButton("💤 10m", callback_data=f"z:{rem_id}:10"),
            InlineKeyboardButton("💤 1h", callback_data=f"z:{rem_id}:60"),
            InlineKeyboardButton("💤 Tomorrow", callback_data=f"z:{rem_id}:tom"),
        ]]
    )


# --- listing --------------------------------------------------------------


async def _send_list(
    update: Update,
    st: State,
    title: str,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    importance: Optional[str] = None,
) -> None:
    now = _now()
    rems = [r for r in await st.db.active() if not importance or r.importance == importance]
    items: list[tuple[datetime, Reminder]] = []
    undated: list[Reminder] = []
    if start is None and end is None:
        # Everything: the current occurrence of each reminder.
        for rem in rems:
            if rem.due_at:
                items.append((rem.due_at, rem))
            else:
                undated.append(rem)
    else:
        lo = start or now
        hi = end or lo + timedelta(days=365)
        for rem in rems:
            items.extend((dt, rem) for dt in scheduler.occurrences(rem, lo, hi, st.tz))
    text = fmt.listing(title, items[:80], st.tz, now, undated)
    await update.effective_message.reply_text(text, parse_mode=HTML)


async def _complete(st: State, rem: Reminder) -> str:
    result = scheduler.mark_done(rem, _now(), st.tz)
    await st.db.save(rem)
    if result == "next":
        return f"✔️ Done. Next: {fmt.when(rem.due_at, st.tz)}"
    if result == "not_due":
        return f"Nothing to complete yet — next is {fmt.when(rem.due_at, st.tz)}. Use /delete {rem.id} to stop it."
    if result == "already":
        return "Already done."
    return "✔️ Done"


# --- message processing ---------------------------------------------------


async def _keep_typing(bot, chat_id: int) -> None:
    """Show "typing…" until cancelled. Telegram clears it after ~5 seconds, and
    a Gemini call can take much longer."""
    while True:
        try:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except TelegramError:
            pass
        await asyncio.sleep(4)


async def _process(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: Optional[str] = None,
    audio: Optional[bytes] = None,
    audio_mime: str = "audio/ogg",
) -> None:
    st = _state(context)
    msg = update.effective_message
    prior = context.user_data.pop("prior", None)
    now = _now()
    typing = asyncio.create_task(_keep_typing(context.bot, msg.chat_id))
    try:
        parsed: ParsedIntent = await st.llm.parse(
            now=now.astimezone(st.tz),
            tz_name=st.tz.key,
            text=text,
            audio=audio,
            audio_mime=audio_mime,
            active=await st.db.active(),
            prior=prior,
        )
    except LLMError as exc:
        log.error("Parsing failed: %s", exc)
        if prior:
            context.user_data["prior"] = prior
        await msg.reply_text("Sorry, I couldn't process that right now. Please try again in a moment.")
        return
    finally:
        typing.cancel()

    said = text or parsed.transcript or ""
    if audio is not None and parsed.transcript:
        await msg.reply_text(f"🎙 <i>{fmt.escape(parsed.transcript)}</i>", parse_mode=HTML)

    if parsed.clarification_needed:
        context.user_data["prior"] = (
            (prior + "\n" if prior else "")
            + f'The user said: "{said}"\nYou asked: "{parsed.clarification_needed}"\n'
            "The user's next message answers that question."
        )
        await msg.reply_text(parsed.clarification_needed)
        return

    if parsed.intent == "create" and parsed.reminders:
        for draft in parsed.reminders:
            rem = build_reminder(draft, st.tz, now, said)
            st.draft_seq += 1
            st.drafts[st.draft_seq] = rem
            text_out = fmt.card(rem, st.tz)
            if rem.due_at and rem.due_at < now and not rem.rrule:
                text_out += "\n⚠️ This time is in the past."
            if draft.recurrence_rrule and rem.due_at and not rem.rrule:
                text_out += (
                    "\n⚠️ I couldn't work out the repeat pattern, so this would fire once only. "
                    "Press Edit and describe it differently."
                )
            await msg.reply_text(text_out, parse_mode=HTML, reply_markup=_draft_keyboard(st.draft_seq))
    elif parsed.intent == "query":
        start = _parse_local(parsed.query_from, st.tz)
        end = _parse_local(parsed.query_to, st.tz)
        title = "Scheduled" if not (start or end) else (
            f"{fmt.when(start or now, st.tz)} – {fmt.when(end, st.tz)}" if end else f"From {fmt.when(start, st.tz)}"
        )
        if parsed.query_importance:
            title += f" ({parsed.query_importance})"
        await _send_list(update, st, title, start, end, parsed.query_importance)
    elif parsed.intent in ("complete", "delete") and parsed.target_ids:
        lines = []
        for rem_id in parsed.target_ids:
            rem = await st.db.get(rem_id)
            if rem is None or rem.status != "active":
                continue
            if parsed.intent == "complete":
                lines.append(f"<b>{fmt.escape(rem.task)}</b>: {await _complete(st, rem)}")
            else:
                rem.status = "cancelled"
                rem.next_fire_at = None
                await st.db.save(rem)
                lines.append(f"🗑 Deleted: <b>{fmt.escape(rem.task)}</b>")
        await msg.reply_text("\n".join(lines) or "I couldn't find that reminder. See /list.", parse_mode=HTML)
    else:
        await msg.reply_text(parsed.reply or "I didn't catch a reminder in that. See /help.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _process(update, context, text=update.effective_message.text)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    media = msg.voice or msg.audio
    try:
        tg_file = await media.get_file()
        data = bytes(await tg_file.download_as_bytearray())
    except TelegramError as exc:
        log.error("Voice download failed: %s", exc)
        await msg.reply_text("I couldn't download that recording. Please try again.")
        return
    await _process(update, context, audio=data, audio_mime=media.mime_type or "audio/ogg")


# --- commands -------------------------------------------------------------


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP, parse_mode=HTML)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_list(update, _state(context), "Scheduled")


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    st = _state(context)
    now = _now()
    end = now.astimezone(st.tz).replace(hour=23, minute=59, second=59)
    await _send_list(update, st, "Today", now, end)


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = _now()
    await _send_list(update, _state(context), "Next 7 days", now, now + timedelta(days=7))


async def _target(update: Update, context: ContextTypes.DEFAULT_TYPE, usage: str) -> Optional[Reminder]:
    arg = (context.args[0] if context.args else "").lstrip("#")
    if not arg.isdigit():
        await update.effective_message.reply_text(f"Usage: {usage}")
        return None
    rem = await _state(context).db.get(int(arg))
    if rem is None or rem.status == "cancelled":
        await update.effective_message.reply_text("No such reminder. See /list for ids.")
        return None
    return rem


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rem = await _target(update, context, "/done <id>")
    if rem:
        result = await _complete(_state(context), rem)
        await update.effective_message.reply_text(f"<b>{fmt.escape(rem.task)}</b>: {result}", parse_mode=HTML)


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rem = await _target(update, context, "/delete <id>")
    if rem:
        rem.status = "cancelled"
        rem.next_fire_at = None
        await _state(context).db.save(rem)
        await update.effective_message.reply_text(f"🗑 Deleted: <b>{fmt.escape(rem.task)}</b>", parse_mode=HTML)


async def cmd_tz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    st = _state(context)
    if not context.args:
        await update.effective_message.reply_text(
            f"Timezone: {st.tz.key}\nChange it with e.g. /tz Europe/Sofia"
        )
        return
    try:
        st.tz = ZoneInfo(context.args[0])
    except (ZoneInfoNotFoundError, ValueError):
        await update.effective_message.reply_text("Unknown timezone. Use an IANA name like Europe/Sofia.")
        return
    await st.db.set_setting("tz", st.tz.key)
    await update.effective_message.reply_text(
        f"Timezone set to {st.tz.key}. Existing reminders keep their absolute time."
    )


# --- buttons --------------------------------------------------------------


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    st = _state(context)
    query = update.callback_query
    if update.effective_user is None or update.effective_user.id != st.settings.allowed_user_id:
        await query.answer()
        return
    action, _, rest = (query.data or "").partition(":")
    original = query.message.text_html if query.message else ""

    if action in ("s", "e", "c"):
        rem = st.drafts.pop(int(rest), None)
        if rem is None:
            await query.answer("This draft has expired. Please send it again.", show_alert=True)
            await query.edit_message_reply_markup(None)
            return
        await query.answer()
        if action == "s":
            now = _now()
            rem.created_at = now
            scheduler.reschedule(rem, now, st.tz)
            await st.db.add(rem)
            await query.edit_message_text(
                f"✅ Saved <code>#{rem.id}</code>\n{fmt.card(rem, st.tz)}", parse_mode=HTML
            )
        elif action == "e":
            context.user_data["prior"] = (
                "The user is correcting this draft reminder:\n"
                + _draft_json(rem, st.tz)
                + "\nTheir next message says what to change. Return intent=create with "
                "the full corrected reminder, keeping everything they did not change."
            )
            await query.edit_message_text(original + "\n\n✏️ Tell me what to change.", parse_mode=HTML)
        else:
            await query.edit_message_text("❌ Cancelled.")
        return

    rem_id, _, arg = rest.partition(":")
    rem = await st.db.get(int(rem_id))
    if rem is None or rem.status == "cancelled":
        await query.answer("This reminder no longer exists.", show_alert=True)
        await query.edit_message_reply_markup(None)
        return
    await query.answer()
    if action == "d":
        note = await _complete(st, rem)
    elif action == "z":
        now = _now()
        if arg == "tom":
            until = fmt.tomorrow_morning(now.astimezone(st.tz)).astimezone(UTC)
        else:
            until = now + timedelta(minutes=int(arg))
        scheduler.snooze(rem, until)
        await st.db.save(rem)
        note = f"💤 Snoozed until {fmt.when(until, st.tz)}"
    else:
        return
    await query.edit_message_text(f"{original}\n\n{note}", parse_mode=HTML)


# --- scheduler tick -------------------------------------------------------


async def tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    st = _state(context)
    now = _now()
    for rem in await st.db.due(now):
        missed = now - rem.next_fire_at > timedelta(minutes=5)
        try:
            await context.bot.send_message(
                st.settings.allowed_user_id,
                fmt.notification(rem, now, st.tz, missed),
                parse_mode=HTML,
                reply_markup=_fired_keyboard(rem.id),
            )
        except TelegramError as exc:
            # Leave it due so the next tick tries again.
            log.error("Could not send reminder %s: %s", rem.id, exc)
            continue
        rem.last_fired_at = now
        scheduler.reschedule(rem, now, st.tz)
        await st.db.save(rem)


def register(app: Application, settings: Settings) -> None:
    me = filters.User(user_id=settings.allowed_user_id)
    app.add_handler(CommandHandler(["start", "help"], cmd_help, filters=me))
    app.add_handler(CommandHandler("list", cmd_list, filters=me))
    app.add_handler(CommandHandler("today", cmd_today, filters=me))
    app.add_handler(CommandHandler("week", cmd_week, filters=me))
    app.add_handler(CommandHandler("done", cmd_done, filters=me))
    app.add_handler(CommandHandler("delete", cmd_delete, filters=me))
    app.add_handler(CommandHandler("tz", cmd_tz, filters=me))
    app.add_handler(MessageHandler(me & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(me & (filters.VOICE | filters.AUDIO), on_voice))
    app.add_handler(CallbackQueryHandler(on_button))
