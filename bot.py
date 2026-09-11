#!/usr/bin/env python3
"""Telegram New-Member Guard Bot — handlers and entrypoint.

Config, age estimation, storage and core decision logic live in bot_core.py.
See bot_core.py's docstring for environment variables.
"""

from bot_core import (
    BOT_TOKEN, ADMIN_CHAT_ID, ADMIN_IDS, GROUP_ID,
    MIN_ACCOUNT_AGE_DAYS, TRUST_AFTER_MESSAGES, AUTO_PASS_OLD_ACCOUNTS,
    REPOST_HELD, DB_PATH,
    estimate_account_age_days, classify_new_user,
    db, init_db, get_user, upsert_user, set_status,
    bump_message_count, hold_message, held_for, clear_held, should_notify,
    log, MUTE_PERMS, UNMUTE_PERMS, describe_user, admin_buttons, is_admin,
    safe_delete, quarantine, approve,
)

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------

async def on_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """chat_member updates fire when someone joins/leaves - the reliable join signal."""
    cm = update.chat_member
    if cm is None:
        return
    chat = update.effective_chat
    if GROUP_ID and chat.id != GROUP_ID:
        return
    old, new = cm.old_chat_member, cm.new_chat_member
    if new.status != "member" or old.status in ("member", "administrator", "creator", "restricted"):
        return  # not a fresh join
    user = new.user
    status, est_days, confidence, spammy = classify_new_user(user)
    if status == "approved":
        upsert_user(user.id, user.username, user.first_name)
        set_status(user.id, "approved")
        log.info("auto-passed old account %s (~%d days)", user.id, est_days)
        return
    reason = "New account (estimated age below threshold)"
    if spammy:
        reason += " + spammy username pattern"
    await quarantine(user, chat, context, reason, est_days, confidence)

async def on_new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fallback join signal (service message) for setups without chat_member updates."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if GROUP_ID and chat.id != GROUP_ID:
        return
    for user in msg.new_chat_members:
        if user.is_bot:
            continue
        existing = get_user(user.id)
        if existing and existing["status"] in ("approved", "banned"):
            continue
        status, est_days, confidence, spammy = classify_new_user(user)
        if status == "approved":
            upsert_user(user.id, user.username, user.first_name)
            set_status(user.id, "approved")
            continue
        reason = "New account (estimated age below threshold)"
        if spammy:
            reason += " + spammy username pattern"
        await quarantine(user, chat, context, reason, est_days, confidence)

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.from_user:
        return
    chat = update.effective_chat
    if chat is None or chat.type not in ("group", "supergroup"):
        return
    if GROUP_ID and chat.id != GROUP_ID:
        return
    user = msg.from_user
    if user.is_bot or msg.sender_chat is not None:  # bots + anonymous/channel posts pass
        return

    row = get_user(user.id)
    if row is None:
        row = upsert_user(user.id, user.username, user.first_name)
        # First time we see this user: apply the account-age gate.
        status, est_days, confidence, spammy = classify_new_user(user)
        if status == "approved":
            set_status(user.id, "approved")
            bump_message_count(user.id)
            return
        await quarantine(user, chat, context, "Young account seen messaging (joined before bot?)", est_days, confidence)
        # fall through: their message gets held below

    status = row["status"]
    if status == "banned":
        await safe_delete(msg)
        return
    if status == "approved":
        bump_message_count(user.id)
        return

    # quarantined or new-but-not-yet-classified: hold the message
    count = bump_message_count(user.id)
    if status == "quarantined":
        await safe_delete(msg)
        text = msg.text or msg.caption or f"[non-text message: {msg.effective_attachment.__class__.__name__}]"
        hold_message(user.id, chat.id, msg.message_id, text)
        if should_notify(user.id):
            held = held_for(user.id)
            try:
                await context.bot.send_message(
                    ADMIN_CHAT_ID,
                    f"HELD (quarantined) {describe_user(user)} in `{chat.id}`\n"
                    f"Latest: {text[:300]}\nTotal held: {len(held)}\n"
                    f"Use /approve {user.id} or the buttons on the earlier notification.",
                    disable_web_page_preview=True,
                    reply_markup=admin_buttons(user.id, chat.id),
                )
            except TelegramError as e:
                log.error("admin notify failed: %s", e)
        # auto-trust after enough clean messages? NO: quarantined users stay held.

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not is_admin(update):
        if q:
            await q.answer("Admins only.", show_alert=True)
        return
    await q.answer()
    try:
        _, action, user_id_s, chat_id_s = (q.data or "").split(":")
        user_id, chat_id = int(user_id_s), int(chat_id_s)
    except (ValueError, AttributeError):
        return
    if action == "approve":
        result = await approve(user_id, chat_id, context, approver=q.from_user.first_name)
    elif action == "mute":
        set_status(user_id, "quarantined")
        try:
            await context.bot.restrict_chat_member(chat_id, user_id, permissions=MUTE_PERMS)
            result = f"Muted user {user_id} indefinitely."
        except TelegramError as e:
            result = f"Mute failed: {e}"
    elif action == "ban":
        set_status(user_id, "banned")
        clear_held(user_id)
        try:
            await context.bot.ban_chat_member(chat_id, user_id)
            result = f"Banned user {user_id}."
        except TelegramError as e:
            result = f"Ban failed: {e} (is the bot admin with ban rights?)"
    else:
        return
    try:
        await context.bot.send_message(ADMIN_CHAT_ID, result)
    except TelegramError:
        pass

def _target_user_id(update: Update, arg: str | None) -> int | None:
    if arg and arg.lstrip("-").isdigit():
        return int(arg)
    reply = update.effective_message.reply_to_message if update.effective_message else None
    if reply and reply.from_user:
        return reply.from_user.id
    return None

async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    uid = _target_user_id(update, " ".join(context.args or []))
    if uid is None:
        await update.effective_message.reply_text("Usage: /approve <user_id> (or reply to their message)")
        return
    chat_id = GROUP_ID or (update.effective_chat.id if update.effective_chat else 0)
    await update.effective_message.reply_text(await approve(uid, chat_id, context, approver=update.effective_user.first_name))

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    uid = _target_user_id(update, " ".join(context.args or []))
    if uid is None:
        await update.effective_message.reply_text("Usage: /ban <user_id> (or reply to their message)")
        return
    set_status(uid, "banned")
    clear_held(uid)
    chat_id = GROUP_ID or (update.effective_chat.id if update.effective_chat else 0)
    try:
        await context.bot.ban_chat_member(chat_id, uid)
        await update.effective_message.reply_text(f"Banned {uid}.")
    except TelegramError as e:
        await update.effective_message.reply_text(f"Ban failed: {e}")

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    uid = _target_user_id(update, " ".join(context.args or []))
    if uid is None:
        await update.effective_message.reply_text("Usage: /check <user_id>")
        return
    days, conf = estimate_account_age_days(uid)
    row = get_user(uid)
    state = row["status"] if row else "unknown"
    held = len(held_for(uid))
    await update.effective_message.reply_text(
        f"User `{uid}`\nEstimated account age: ~{days} days ({conf})\n"
        f"Bot status: {state}\nHeld messages: {held}"
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    with db() as c:
        stats = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM users GROUP BY status")}
        held = c.execute("SELECT COUNT(*) n FROM held_messages").fetchone()["n"]
    await update.effective_message.reply_text(
        f"Users: {dict(stats) or 'none yet'}\nHeld messages: {held}"
    )

async def cmd_held(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    uid = _target_user_id(update, " ".join(context.args or []))
    if uid is None:
        await update.effective_message.reply_text("Usage: /held <user_id>")
        return
    rows = held_for(uid)
    if not rows:
        await update.effective_message.reply_text("No held messages for that user.")
        return
    out = "\n---\n".join(r["text"][:500] for r in rows[:10])
    await update.effective_message.reply_text(f"Held for `{uid}` ({len(rows)}):\n{out}", disable_web_page_preview=True)

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

async def post_init(application: Application) -> None:
    me = await application.bot.get_me()
    log.info("Logged in as @%s", me.username)
    try:
        await application.bot.send_message(
            ADMIN_CHAT_ID, "New-member guard bot is online. Make sure I am an ADMIN in the group with:\n- Delete messages\n- Restrict/ban members\nPrivacy mode is irrelevant for admins; I see all messages."
        )
    except TelegramError as e:
        log.error("Could not message ADMIN_CHAT_ID (%s). Double-check the id.", e)

def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    init_db()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_chat_members))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, on_message))
    app.add_handler(CommandHandler("approve", cmd_approve, filters.ChatType.PRIVATE | filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("ban", cmd_ban, filters.ChatType.PRIVATE | filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("check", cmd_check, filters.ChatType.PRIVATE | filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("status", cmd_status, filters.ChatType.PRIVATE | filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("held", cmd_held, filters.ChatType.PRIVATE | filters.ChatType.GROUPS))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^act:"))
    # chat_member updates must be requested explicitly or join tracking won't fire
    app.run_polling(allowed_updates=["message", "chat_member", "callback_query"])

if __name__ == "__main__":
    main()
