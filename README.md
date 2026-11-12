# Telegram reminders bot

A personal Telegram bot. Send it a text or voice message in plain language;
a Gemini Flash model works out **what** needs doing, **when**, **how often**
it repeats, **when to remind you** and **how important** it is. The bot then
sends the reminders and can tell you what is scheduled.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Get a free Gemini API key at <https://aistudio.google.com/apikey>.
3. Find your numeric Telegram user ID (e.g. from [@userinfobot](https://t.me/userinfobot)).
   The bot ignores everyone else.
4. Copy `.env.example` to `.env` and fill it in.
5. Open your bot in Telegram and press **Start** (a bot cannot message you first).

## Run

With Docker:

```bash
docker compose up -d --build
```

Or directly (Python 3.11+):

```bash
python -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python -m bot
```

`reminder-bot.service` is a systemd unit for running it as a service.
Reminders live in a SQLite file (`data/reminders.db`), so they survive
restarts; anything that came due while the bot was down is sent when it is
back up.

## Use

- *"remind me to call mom tomorrow evening"*
- *"pay rent on the 1st every month, remind me 3 days before, important"*
- *"take pills every day at 8, keep bugging me every 10 minutes"*
- a voice message saying the same kind of thing

The bot shows what it understood with **Save / Edit / Cancel** buttons. Each
reminder it sends has **Done** and **Snooze** buttons.

Ask *"what do I have this week?"* or use the commands:

| Command | |
|---|---|
| `/list` | everything upcoming |
| `/today`, `/week` | today / next 7 days |
| `/done <id>` | mark as done |
| `/delete <id>` | cancel a reminder |
| `/tz <Area/City>` | set your timezone |

If you don't say how you want to be reminded, importance decides:

| Importance | Reminders |
|---|---|
| low, medium | once, at the due time |
| high | 1 day before, 1 hour before, at the due time, then hourly until done |
| urgent | at the due time, then every 15 minutes until done |

## Tests

```bash
pip install -r requirements-dev.txt
```

```bash
pytest
```

`GEMINI_API_KEY=... pytest -m live` additionally checks parsing against the real API.
