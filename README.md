# Internship Alerts

Watches two community internship repos every 30 minutes via GitHub Actions and
alerts when a new listing matches the watchlist:
[SimplifyJobs/Summer2026-Internships](https://github.com/SimplifyJobs/Summer2026-Internships)
and [vanshb03/Summer2026-Internships](https://github.com/vanshb03/Summer2026-Internships)
(the CSCareers community repo — smaller and partly distinct, so it catches
postings Simplify misses). Both use the same `listings.json` schema; `check.py`
reads them in one pass and dedups the same job across the two by company+title.
Add or swap repos in the `SOURCES` list at the top of `check.py`. Total running
cost: $0.

Alerts go to **two channels**: Discord (rich message with clickable role links)
and [Poke](https://poke.com) (a plain text describing company/role/season, no
link). Set `DISCORD_WEBHOOK_URL` and/or `POKE_API_KEY` — whichever is present
gets sent; the other is skipped. Get a Poke key at poke.com/kitchen → API Keys.

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

## Company job-board watcher

Simplify's community repo sometimes lags a company's own careers page, or
never lists smaller companies at all. `check_companies.py` (hourly) reads
current postings directly from a company's own job board API instead of
waiting on Simplify, for every watchlist company that turns out to be on a
supported job board: Greenhouse, Lever, Ashby, SmartRecruiters, or Workday.

`resolve_companies.py` figures out which board (if any) each watchlist company
uses. For the four ATSs it guesses common slug conventions (e.g. "Capital One"
-> `capitalone`) and verifies against the live API. Workday needs three
coordinates (tenant + datacenter + an arbitrary site name) that can't be
guessed as one slug, so it's probed separately: the job API returns HTTP 404
for a valid tenant+datacenter with a wrong site but 422 for a wrong
tenant/datacenter, which pins the first two cheaply, then common site-name
patterns are tried for the third (this reaches most of the big companies on
custom-looking Workday sites — Adobe, Salesforce, NVIDIA, Capital One, ...).

A few big companies aren't on any standard board (fully custom career sites).
Those get a **bespoke integration** against the company's own JSON API, matched
by name in `CUSTOM_COMPANIES` (in `resolve_companies.py`) rather than
auto-resolved — Amazon (`amazon.jobs`) is the first. Adding another is one
fetcher in `platforms.py` plus a name entry; no headless browser, so it stays
as light as the platform integrations.

Results are cached in `state/company_platforms.json`; still-unresolved
companies (custom sites without a known API) are retried automatically after 7
days in case they adopt a supported board later. Each run resolves at most
`MAX_NEW_PER_RUN` new companies so a batch of slow Workday probes can't blow the
hourly job's time budget; the rest are picked up on later runs. Resolution runs
automatically as part of `check_companies.py`, but can also be run standalone to
see the hit/miss breakdown:

```
python resolve_companies.py
```

Alerts fire only for postings whose title looks like a software-engineering
internship (`intern`/`internship` + a SWE-ish keyword — see `SWE_RE` /
`INTERN_RE` in `check_companies.py` if you want to loosen or tighten that).

## Junior program watcher

`check_programs.py` runs daily (~9:17 AM ET) and alerts when a junior-program
page changes (e.g. applications open): Microsoft Explore, Uber University,
Google STEP, Amazon University/Propel, Jane Street JSIP. Edit `programs.json`
to add/remove pages. Note some sites (Meta, Bloomberg, Citadel) block
automated checks and can't be watched this way.

A page "change" is any edit to the page's visible text, so expect occasional
alerts for cosmetic edits — the message just says which page to go look at.

## Local test

```
set DISCORD_WEBHOOK_URL=...   (omit for dry-run: messages print to console)
pip install -r requirements.txt
python check.py
python check_companies.py    (first run resolves the whole watchlist -- slow)
```
