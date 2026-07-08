# Internship Alerts

Watches [SimplifyJobs/Summer2026-Internships](https://github.com/SimplifyJobs/Summer2026-Internships)
every 30 minutes via GitHub Actions and sends a Discord message when a new
listing matches the watchlist. Total running cost: $0.

## Setup (one time, ~5 min)

1. **Discord server**: in the Discord app, create your own server (the `+`
   button) with a channel like `#internships`. Just you in it is fine.
2. **Webhook**: channel settings (gear icon) → Integrations → Webhooks →
   New Webhook → Copy Webhook URL.
3. **Phone notifications**: install Discord on your phone, then long-press the
   channel → Notification Settings → **All Messages** (webhook posts don't
   @mention you, so the default "only @mentions" setting would stay silent).
4. **GitHub secret**: in this repo → Settings → Secrets and variables →
   Actions, add `DISCORD_WEBHOOK_URL`.
5. Trigger the workflow once manually (Actions tab → "Check for new
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
set DISCORD_WEBHOOK_URL=...   (omit for dry-run: messages print to console)
pip install -r requirements.txt
python check.py
```
