# Telegram New-Member Guard Bot

A strict-quarantine moderation bot for Telegram groups, built for the classic
spam pattern: a wave of users joins from a YouTube/channel link, and the ones
that are actually fresh accounts post spam — or worse, put their spam links in
their **bio** and never post at all, so a normal ban never cleans them up.

## How it behaves

- **Account-age gate.** Telegram never tells bots when an account was created,
  so the bot estimates account age from the numeric user ID (IDs are issued
  roughly chronologically). Anyone estimated to be under
  `MIN_ACCOUNT_AGE_DAYS` (default 30) is treated as a fresh/suspicious account.
- **Quarantine.** Suspicious accounts are muted the instant they join, and any
  message they still manage to send is deleted and *held* (stored, shown to
  admins — nothing reaches the group).
- **Admin review.** You get one notification per user with buttons:
  **Approve** (unmutes, and re-posts their held messages into the group),
  **Mute**, or **Ban**.
- **Existing members are left alone.** Anyone who has sent
  `TRUST_AFTER_MESSAGES` (default 10) messages, and old-looking accounts when
  `AUTO_PASS_OLD_ACCOUNTS=1`, pass untouched — the "strict for new, invisible
  for regulars" behaviour.
- **Bio check (optional `bio_guard.py`).** The Bot API cannot read bios at
  all — that's why spammers hide there. The companion userbot runs on your own
  account, reads each new joiner's bio, and if it contains links or promo
  keywords it mutes them through the guard bot and pings admins.

## Quick start

1. **Create the bot.** Message [@BotFather](https://t.me/BotFather) → `/newbot`.
   Copy the token.
2. **Add the bot to your group as ADMIN** with these permissions:
   *Delete messages* and *Restrict/ban members*. (Being an admin also means it
   sees every message — no privacy-mode tweaks needed.)
3. **Configure.**
   ```bash
   cp .env.example .env    # then edit it
   pip install -r requirements.txt
   ```
   Minimal required values: `BOT_TOKEN`, `ADMIN_CHAT_ID`, `GROUP_ID`.
   Find your numeric chat id: forward a group message to `@userinfobot`,
   or add the bot and watch its startup log.
   Load the .env before running, e.g.:
   `export $(grep -v '^#' .env | xargs)`

4. **Run the guard bot.**
   ```bash
   python bot.py
   ```
   It posts an "online" message to your admin chat. Test with
   `/check <any_user_id>` inside that chat.

### Commands (admins only)

| Command | What it does |
|---|---|
| `/approve <user_id>` or reply | Unmute + release held messages |
| `/ban <user_id>` or reply | Ban from the group |
| `/check <user_id>` | Estimated account age + bot status |
| `/held <user_id>` | Show that user's held messages |
| `/status` | Quarantine/held counters |

The **Approve / Mute / Ban** inline buttons on quarantine notifications do the
same thing.

### Calibrating the age table (important!)

The ID → age mapping is a heuristic. Open `bot.py` and find
`ACCOUNT_AGE_ANCHORS` — a list of `(user_id, approx_date)` pairs. To tune it:
ask two or three members you trust when they created their account, run
`/check` on them, and nudge the anchors. Beyond the last anchor the estimate is
*extrapolated* (marked as such by `/check`) — that's where fresh 2025/2026
accounts land, so keep that last anchor updated once in a while. If you prefer
certainty over heuristics, set `AUTO_PASS_OLD_ACCOUNTS=0` to quarantine
**every** new joiner until an admin approves them.

## Deploy on GitHub Actions

GitHub Actions is a CI/CD runner, not a hosting platform — jobs are
time-limited (6 h max) and storage is ephemeral. This project handles that
with a **scheduled workflow** that starts the bot every 5 hours and
restores `guard.db` from the Actions cache between runs. It's not a 24/7
always-on host, but it's free for public repos and good enough for a
moderation bot.

**Trade-offs you should know:**

- ~5 min restart gap every 5 h (Telegram stores bot updates for 24 h, so
  messages during the gap are delivered on reconnect — nothing is lost).
- Github may delay the cron by up to ~30 min during peak load.
- The cache for `guard.db` can be evicted after ~7 days of no runs. If that
  happens, the bot starts fresh (all users re-evaluated from scratch).
- `bio_guard.py` is **not** included in the Actions deployment — it needs an
  interactive Telethon login (phone + code) that doesn't fit CI. Run it
  separately on your own machine if you need bio scanning.

### Setup

1. Push this repo to GitHub (public = unlimited Actions minutes).
2. Go to **Settings → Secrets and variables → Actions**.
3. Under **Secrets**, add:
   | Secret | Value |
   |---|---|
   | `BOT_TOKEN` | your BotFather token |
   | `ADMIN_CHAT_ID` | numeric id of your admin chat |
   | `GROUP_ID` | numeric id of the group to moderate |
   | `ADMIN_IDS` | comma-separated admin user ids (can be empty) |
4. Under **Variables** (optional, the bot has sensible defaults):
   `MIN_ACCOUNT_AGE_DAYS`, `TRUST_AFTER_MESSAGES`,
   `AUTO_PASS_OLD_ACCOUNTS`, `REPOST_HELD`.
5. Go to **Actions** tab → **Run Guard Bot** → **Enable workflows**.
6. Click **Run workflow** to start it immediately, or wait for the next
   scheduled run.

The workflow file is at `.github/workflows/run-bot.yml`. To change the run
interval, edit the `cron` line (remember GitHub's minimum cron granularity
is 5 minutes, and shorter intervals burn more minutes on private repos).

### Monitoring

- The Actions tab shows each run's logs (bot output goes to stdout).
- The bot posts an "online" message to your admin chat at the start of each
  run, so you can see when it restarts.
- If a run fails, GitHub can email you (Settings → Notifications).

### Better alternatives for production

If you need true 24/7 uptime with no restart gaps, deploy on a real host:

- **Railway / Render / Fly.io** — free tiers, supports always-on workers,
  just push the repo and set the same env vars. Swap SQLite for PostgreSQL
  (change the `db()` function) for persistent state.
- **A cheap VPS** (Hetzner, etc.) — `systemctl` service, runs forever,
  $3–5/month.
- **Self-hosted GitHub Actions runner** — if you already have a server,
  register it as a self-hosted runner and the workflow runs on your machine
  with no 6-hour limit.

## Bio guard (optional, recommended for your use-case)

Because banned spammers put links in their bio, this is the piece that catches
them — and it requires *your own account*, since bots physically cannot read
bios.

1. Get `TELETHON_API_ID` / `TELETHON_API_HASH` from
   <https://my.telegram.org> → API development tools.
2. Make sure your account is a member of the group.
3. Run:
   ```bash
   python bio_guard.py
   ```
   First run asks for your phone + login code once, then saves a session file
   (`bio_guard_session.session`) — keep that file private, it *is* your login.
4. From then on: new member joins → their bio is read → any link/t.me/promo
   keyword → instantly muted (through the guard bot) + admin notification with
   the bio text and `/approve` / `/ban` hints.

Keep it running next to `bot.py` (two terminals / two systemd services).
`bio_guard.py` needs no group admin rights for your account; the muting is
done via the bot.

## Notes & limits

- The mute-at-join is the primary quarantine; message-holding is a safety net
  for members who joined before the bot was installed or if a mute fails.
- Held messages are re-posted **by the bot** with attribution — Telegram gives
  no way to resend a message as the original user.
- `guard.db` (SQLite) is the bot's memory: back it up, delete it to reset.
- Telegram ToS: userbots (Telethon on your own account) are allowed, but keep
  the session file secret and don't automate your own account for anything
  beyond reading what you'd see anyway.
