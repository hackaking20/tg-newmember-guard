#!/usr/bin/env python3
"""
Telegram New-Member Guard Bot
=============================
Strict-quarantine moderation bot for Telegram groups.

What it does
------------
1.  ACCOUNT-AGE GATE: Telegram does not expose account creation dates to bots,
    so we estimate a user's account age from their numeric user ID (IDs are
    handed out roughly chronologically). Accounts estimated younger than
    MIN_ACCOUNT_AGE_DAYS are treated as "new/suspicious".

2.  QUARANTINE: Suspicious accounts are muted the moment they join (and any
    message they still manage to send is deleted and *held*). Admins get a
    notification with inline buttons:
        [Approve]  -> unmutes; held messages are re-posted
        [Mute]     -> keeps them silenced indefinitely
        [Ban]      -> removes them from the group

3.  TRUST BY ACTIVITY: members who have already sent TRUST_AFTER_MESSAGES
    clean messages are never quarantined (existing members are left alone).

4.  OPTIONAL BIO CHECK: the Bot API cannot read user bios. A companion
    userbot script (bio_guard.py, Telethon-based) watches joins, scans the
    new member's bio for spam links, and if found mutes them + notifies the
    admin chat via this bot's token. Run it on your own account; see README.

Setup (env vars / .env file)
----------------------------
BOT_TOKEN        (required) token from @BotFather
ADMIN_CHAT_ID    (required) chat where notifications/buttons go (your admin group or your own user id)
ADMIN_IDS        (optional) comma-separated admin user ids allowed to use commands
GROUP_ID         (optional) if set, the bot only moderates that chat
MIN_ACCOUNT_AGE_DAYS  (default 30) accounts estimated younger than this are quarantined
TRUST_AFTER_MESSAGES  (default 10)  messages before a member is auto-trusted
AUTO_PASS_OLD_ACCOUNTS (default 1)  legit-looking old accounts skip quarantine
REPOST_HELD      (default 1)        re-post held messages to the group after approval
DB_PATH          (default guard.db)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime, timezone

from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)

def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off")

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

BOT_TOKEN = _env("BOT_TOKEN")
ADMIN_CHAT_ID = _env_int("ADMIN_CHAT_ID", 0)
ADMIN_IDS = {int(x) for x in _env("ADMIN_IDS").replace(" ", "").split(",") if x}
GROUP_ID = _env_int("GROUP_ID", 0)  # 0 = moderate every group the bot is in
MIN_ACCOUNT_AGE_DAYS = _env_int("MIN_ACCOUNT_AGE_DAYS", 30)
TRUST_AFTER_MESSAGES = _env_int("TRUST_AFTER_MESSAGES", 10)
AUTO_PASS_OLD_ACCOUNTS = _env_bool("AUTO_PASS_OLD_ACCOUNTS", True)
REPOST_HELD = _env_bool("REPOST_HELD", True)
DB_PATH = _env("DB_PATH", "guard.db")

NOTIFY_COOLDOWN_SEC = 600  # re-notify admins about the same quarantined user at most every 10 min

if not BOT_TOKEN or not ADMIN_CHAT_ID:
    raise SystemExit("BOT_TOKEN and ADMIN_CHAT_ID must be set (see .env.example).")

# --------------------------------------------------------------------------
# Account-age estimation from user ID
# --------------------------------------------------------------------------
# APPROXIMATE anchors: (user_id, approximate creation date).
# Telegram IDs are issued roughly chronologically. These anchors are rough
# public estimates - calibrate them for your group with /check on users whose
# join date you actually know, then edit this table.
ACCOUNT_AGE_ANCHORS: list[tuple[int, date]] = [
    (100_000_000, date(2013, 5, 1)),
    (500_000_000, date(2014, 10, 1)),
    (1_000_000_000, date(2016, 3, 1)),
    (2_000_000_000, date(2017, 11, 1)),
    (5_000_000_000, date(2019, 10, 1)),
    (7_500_000_000, date(2020, 10, 1)),
    (10_000_000_000, date(2021, 6, 1)),
    (15_000_000_000, date(2022, 6, 1)),
    (20_000_000_000, date(2022, 12, 1)),
    (55_000_000_000, date(2023, 6, 1)),
    (65_000_000_000, date(2024, 3, 1)),
    (73_000_000_000, date(2024, 12, 1)),
    (78_000_000_000, date(2025, 12, 1)),
]

def estimate_account_age_days(user_id: int) -> tuple[int, str]:
    """Return (estimated_age_in_days, confidence) from the user ID.
    Confidence is 'exact-anchor', 'interpolated' or 'extrapolated' (beyond the
    last anchor - treat with care and recalibrate the anchors)."""
    today = datetime.now(timezone.utc).date()
    if user_id <= 0:
        return 10_000, "interpolated"
    if user_id <= ACCOUNT_AGE_ANCHORS[0][0]:
        return (today - ACCOUNT_AGE_ANCHORS[0][1]).days, "interpolated"
    for (id_a, d_a), (id_b, d_b) in zip(ACCOUNT_AGE_ANCHORS, ACCOUNT_AGE_ANCHORS[1:]):
        if id_a <= user_id <= id_b:
            frac = (user_id - id_a) / (id_b - id_a)
            est = d_a + (d_b - d_a) * frac
            return (today - est).days, "interpolated"
    # beyond the last anchor: extrapolate with the slope of the last segment
    (id_a, d_a), (id_b, d_b) = ACCTõnt_AGE_ANCHORS[-2], ACCOUNT_AGE_ANCHORS[-1]
    ids_per_day = (id_b - id_a) / max((d_b - d_a).days, 1)
    from datetime import timedelta
    est = d_b + timedelta(days=(user_id - id_b) / ids_per_day)
    return (today - est).days, "extrapolated"

# Cheap username spam signal (used only as a tiebreaker, never as sole reason)
SPAMMY_USERNAME_RE = re.compile(r"(casino|promo|crypto|earn|whatsapp|channel|admin)", re.I)

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                first_seen_at INTEGER,
                message_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'new',          -- new|quarantined|approved|banned
                approved_at INTEGER
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS held_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chat_id INTEGER,
                message_id INTEGER,
                text TEXT,
                captured_at INTEGER
            )"""
        )
        c.execute("CREATE TABLE IF NOT EXISTS notify_state (user_id INTEGER PRIMARY KEY, last_notify INTEGER)")

def get_user(user_id: int) -> sqlite3.Row | None:
    with db() as c:
        return c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

def upsert_user(user_id: int, username: str | None, first_name: str | None) -> sqlite3.Row:
    with db() as c:
        c.execute(
            """INSERT INTO users (user_id, username, first_name, first_seen_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username = COALESCE(excluded.username, username),
                 first_name = COALESCE(excluded.first_name, first_name)""",
            (user_id, username, first_name, int(time.time())),
        )
        return c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

def set_status(user_id: int, status: str) -> None:
    with db() as c:
        c.execute(
            "UPDATE users SET status = ?, approved_at = ? WHERE user_id = ?",
            (status, int(time.time()) if status == "approved" else None, user_id),
        )

def bump_message_count(user_id: int) -> int:
    with db() as c:
        c.execute("UPDATE users SET message_count = message_count + 1 WHERE user_id = ?", (user_id,))
        row = c.execute("SELECT message_count FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["message_count"] if row else 0

def hold_message(user_id: int, chat_id: int, message_id: int, text: str) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO held_messages (user_id, chat_id, message_id, text, captured_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, chat_id, message_id, text[:3500], int(time.time())),
        )

def held_for(user_id: int) -> list[sqlite3.Row]:
    with db() as c:
        return c.execute(
            "SELECT * FROM held_messages WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()

def clear_held(user_id: int) -> None:
    with db() as c:
        c.execute("DELETE FROM held_messages WHERE user_id = ?", (user_id,))

def should_notify(user_id: int) -> bool:
    now = int(time.time())
    with db() as c:
        row = c.execute("SELECT last_notify FROM notify_state WHERE user_id = ?", (user_id,)).fetchone()
        if row and now - row["last_notify"] < NOTIFY_COOLDOWN_SEC:
            return False
        c.execute(
            "INSERT INTO notify_state (user_id, last_notify) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_notify = ?",
            (user_id, now, now),
        )
        return True

# --------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------

[remainder of bot.py content for brevity]