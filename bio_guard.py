#!/usr/bin/env python3
"""
Bio Guard - companion userbot for the New-Member Guard Bot
==========================================================
WHY THIS EXISTS: Telegram's Bot API gives bots NO access to user bios.
Spam-banned users often return with a fresh account and put their spam links
(t.me/..., URLs, promo text) in their BIO instead of in messages. To read
bios you must run a MTProto user session (your own account) via Telethon.

This script:
1. Logs into YOUR account (a session file, not your password, after first run).
2. Watches the target group for new members.
3. Reads each new member's bio.
4. If the bio contains links / spam keywords -> calls the Guard Bot's API to
   MUTE them immediately and notifies the admin chat for review.

Your account only needs to be a MEMBER of the group (to see joins and bios);
the muting is performed by the BOT, which must be an admin.

Setup (env vars / .env - shared with the main bot)
--------------------------------------------------
GUARD_BOT_TOKEN   the @BotFather token of the guard bot (used to mute/notify)
ADMIN_CHAT_ID     where notifications go
GROUP_ID          the group to watch (required here)
BIO_SPAM_PATTERNS optional extra regexes, comma-separated (default: any URL or t.me link)
TELETHON_API_ID / TELETHON_API_HASH  from https://my.telegram.org
SESSION_NAME      (default bio_guard_session) session file name
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import urllib.request
import urllib.parse
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.tl.functions.users import GetFullUserRequest

# ---- config ---------------------------------------------------------------

BOT_TOKEN = os.environ.get("GUARD_BOT_TOKEN") or os.environ.get("BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0") or 0)
GROUP_ID = int(os.environ.get("GROUP_ID", "0") or 0)
API_ID = int(os.environ.get("TELETHON_API_ID", "0") or 0)
API_HASH = os.environ.get("TELETHON_API_HASH", "")
SESSION_NAME = os.environ.get("SESSION_NAME", "bio_guard_session")
LOG_ALL_JOINS = os.environ.get("LOG_ALL_JOINS", "0") == "1"

DEFAULT_PATTERNS = [
    r"https?://",          # any URL
    r"t\.me/\S+",          # any telegram link (channels, invites, DM links)
    r"telegram\.me/\S+",
    r"(?i)\b(casino|promo|earn|profit|signals|whatsapp|drug|weed|kush|loan)\b",
]
EXTRA = [p for p in os.environ.get("BIO_SPAM_PATTERNS", "").split("|") if p]
PATTERNS = [re.compile(p) for p in DEFAULT_PATTERNS + EXTRA]

if not (BOT_TOKEN and ADMIN_CHAT_ID and GROUP_ID and API_ID and API_HASH):
    raise SystemExit(
        "Set GUARD_BOT_TOKEN (or BOT_TOKEN), ADMIN_CHAT_ID, GROUP_ID, "
        "TELETHON_API_ID, TELETHON_API_HASH - see .env.example"
    )

log = logging.getLogger("bio_guard")

# ---- Bot API helper (mute + notify via the guard bot) ----------------------

def bot_api(method: str, **params) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}?" + urllib.parse.urlencode(
        {k: v for k, v in params.items() if v is not None}
    )
    with urllib.request.urlopen(url, timeout=15) as resp:
        import json
        return json.loads(resp.read())

EMPTY_PERMS = '{"can_send_messages":false,"can_send_other_messages":false,"can_add_web_page_previews":false,"can_send_polls":false}'

def matches_spam(bio: str | None) -> list[str]:
    if not bio:
        return []
    hits = []
    for rx in PATTERNS:
        m = rx.search(bio)
        if m:
            hits.append(m.group(0)[:60])
    return hits

# ---- main ------------------------------------------------------------------

async def main() -> None:
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()  # first run asks for phone + code, then saves the session
    me = await client.get_me()
    log.info("Bio guard running as %s", me.username or me.id)

    entity = await client.get_entity(GROUP_ID)

    @client.on(events.ChatAction)
    async def on_join(event):
        if event.chat_id != entity.id:
            return
        users = []
        if event.user_joined:
            users = [await event.get_user()]
        elif event.user_added:
            added = await event.get_users()
            users = added if added else []
        for user in users:
            if not user or getattr(user, "bot", False):
                continue
            try:
                full = await client(GetFullUserRequest(user.id))
                bio = full.full_user.about
            except Exception as e:
                log.warning("could not fetch bio for %s: %s", user.id, e)
                bio = None
            uname = f"@{user.username}" if user.username else "no username"
            if LOG_ALL_JOINS:
                bot_api("sendMessage", chat_id=ADMIN_CHAT_ID,
                        text=f"Join: {user.first_name} {uname} `{user.id}`\nBio: {bio or '(empty)'}")
            hits = matches_spam(bio)
            if hits:
                try:
                    bot_api("restrictChatMember", chat_id=entity.id, user_id=user.id,
                            permissions=EMPTY_PERMS)
                except Exception as e:
                    log.error("mute via bot failed (bot admin?): %s", e)
                bot_api(
                    "sendMessage", chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"SPAM BIO DETECTED\n"
                        f"User: {user.first_name} {uname} `{user.id}`\n"
                        f"Bio: {(bio or '')[:300]}\n"
                        f"Matched: {', '.join(hits)}\n"
                        f"Already muted. Review, then /approve {user.id} or /ban {user.id} in the guard bot."
                    ),
                )

    log.info("Watching group %s for joins...", entity.id)
    await client.run_until_disconnected()

if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    asyncio.run(main())
