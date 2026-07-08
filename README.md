# Internship Alerts

Watches [SimplifyJobs/Summer2026-Internships](https://github.com/SimplifyJobs/Summer2026-Internships)
every 30 minutes via GitHub Actions and sends a Telegram message when a new
listing matches the watchlist. Total running cost: $0.

## Setup (one time, ~5 min)

1. **Telegram bot**: message [@BotFather](https://t.me/BotFather) → `/newbot` →
   copy the token.
2. **Chat ID**: send any message to your new bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the
   `chat.id` number.
3. **GitHub secrets**: in this repo → Settings → Secrets and variables →
   Actions, add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
4. Trigger the workflow once manually (Actions tab → "Check for new
   internships" → Run workflow). The first run seeds the seen-list and sends a
   "bot is live" message — no flood of old listings.

## Watchlist

Edit `watchlist.txt` — one company or keyword per line, matched
case-insensitively against company name + role title.

**Google Sheet instead**: File → Share → Publish to web → CSV (first column =
keywords), then add the URL as a `WATCHLIST_CSV_URL` secret. The file is then
ignored; edit the sheet from your phone and the next run picks it up.

## Local test

```
set TELEGRAM_BOT_TOKEN=...   (omit for dry-run: messages print to console)
set TELEGRAM_CHAT_ID=...
pip install -r requirements.txt
python check.py
```
