"""Core module: config, account-age estimation, SQLite storage, helpers."""

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
    (id_a, d_a), (id_b, d_b) = ACCOUNT_AGE_ANCHORS[-2], ACCOUNT_AGE_ANCHORS[-1]
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
# --------------------------------------------------------------------------

log = logging.getLogger("guard")

MUTE_PERMS = ChatPermissions.no_permissions()
UNMUTE_PERMS = ChatPermissions.all_permissions()

def describe_user(u) -> str:
    uname = f"@{u.username}" if u.username else "no username"
    return f"[{u.first_name or 'User'}](tg://user?id={u.id}) `{uname}` `{u.id}`"

def admin_buttons(user_id: int, chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Approve", callback_data=f"act:approve:{user_id}:{chat_id}"),
            InlineKeyboardButton("Mute", callback_data=f"act:mute:{user_id}:{chat_id}"),
            InlineKeyboardButton("Ban", callback_data=f"act:ban:{user_id}:{chat_id}"),
        ]
    ])

def is_admin(update: Update) -> bool:
    """Commands: allowed from configured ADMIN_IDS, or from anyone in the admin chat."""
    if not update.effective_user:
        return False
    if update.effective_user.id in ADMIN_IDS:
        return True
    return update.effective_chat is not None and update.effective_chat.id == ADMIN_CHAT_ID

async def safe_delete(message) -> bool:
    try:
        await message.delete()
        return True
    except (BadRequest, Forbidden, TelegramError) as e:
        log.debug("could not delete message: %s", e)
        return False

# --------------------------------------------------------------------------
# Core decision logic
# --------------------------------------------------------------------------

def classify_new_user(user) -> tuple[str, int, str, bool]:
    """Returns (status, est_age_days, confidence, looks_spammy) for a joining user."""
    est_days, confidence = estimate_account_age_days(user.id)
    looks_spammy = bool(user.username and SPAMMY_USERNAME_RE.search(user.username))
    if AUTO_PASS_OLD_ACCOUNTS and est_days >= MIN_ACCOUNT_AGE_DAYS and not looks_spammy:
        return "approved", est_days, confidence, looks_spammy
    return "quarantined", est_days, confidence, looks_spammy

async def quarantine(user, chat, context, reason: str, est_days: int = 0, confidence: str = "") -> None:
    """Mute a user, record status, notify admins with action buttons."""
    row = upsert_user(user.id, user.username, user.first_name)
    if row["status"] in ("approved", "banned"):
        return
    set_status(user.id, "quarantined")
    try:
        await context.bot.restrict_chat_member(chat.id, user.id, permissions=MUTE_PERMS)
    except TelegramError as e:
        log.warning("mute failed for %s: %s (is the bot admin?)", user.id, e)
    if should_notify(user.id):
        held = held_for(user.id)
        text = (
            f"QUARANTINED {describe_user(user)}\n"
            f"Reason: {reason}\n"
            f"Estimated account age: ~{est_days} days ({confidence})\n"
            + (f"Held messages so far: {len(held)}\n" if held else "")
            + "Bio check: run bio_guard.py alongside this bot for bio scanning."
        )
        try:
            await context.bot.send_message(
                ADMIN_CHAT_ID, text,
                reply_markup=admin_buttons(user.id, chat.id),
                disable_web_page_preview=True,
            )
        except TelegramError as e:
            log.error("admin notify failed: %s", e)

async def approve(user_id: int, chat_id: int, context, approver=None) -> str:
    set_status(user_id, "approved")
    try:
        await context.bot.restrict_chat_member(chat_id, user_id, permissions=UNMUTE_PERMS)
    except TelegramError as e:
        log.warning("unmute failed for %s: %s", user_id, e)
    reposted = 0
    if REPOST_HELD:
        for m in held_for(user_id):
            try:
                await context.bot.send_message(
                    m["chat_id"],
                    f"Quarantined message from `{m['user_id']}` (approved):\n\n{m['text']}",
                )
                reposted += 1
            except TelegramError as e:
                log.warning("repost failed: %s", e)
        clear_held(user_id)
    who = f" by {approver}" if approver else ""
    return f"Approved user {user_id}{who}. Unmuted, {reposted} held message(s) re-posted."
