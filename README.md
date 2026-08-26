# Internship Alerts

Watches two community internship repos, plus company job boards directly, every
30 minutes via GitHub Actions and alerts when a new listing matches the
watchlist:
[SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships)
and
[vanshb03/Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships)
(the CSCareers community repo). Both publish the same schema and are deduped
against each other. Simplify is much the larger feed (~14.6k listings vs ~330)
and neither is a superset of the other, so keep both.

**Only Summer 2027, US roles** are alerted on — see `is_target_season` /
`is_us_location` in `check.py`. Community-repo listings are filtered by their
`terms`/`season` and `locations` fields; direct company-board postings
(`check_companies.py`) are filtered by an explicit year mention in the title
(untitled-year postings pass through, since a board only ever lists what's
currently open) and by whatever location/country field each ATS's API exposes
(exact country code where available, free-text heuristic otherwise). Update
`TARGET_YEAR`/`TARGET_SEASON`/`US_STATE_ABBRS`/`NON_US_HINTS` in `check.py`
next cycle or if you want to loosen the location rule.

The main check runs in **one workflow** (`.github/workflows/check-all.yml`) every
30 min: `check.py` (repos) then `check_companies.py` (company boards, fetched
concurrently), a single checkout/pip/commit. GitHub bills a 1-minute minimum per
run, so run *count* is what costs; on a **private** repo, drop the frequency (or
consolidate more) to stay under the free 2,000 Actions-minutes/month. This repo
runs **public**, where Actions is free and unlimited — hence the 30-min main
check plus the 15-min priority fast lane below.

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
5. Trigger the workflow once manually (Actions tab → "Check internships" → Run
   workflow). The first run seeds the seen-list and sends a "bot is live"
   message — no flood of old listings.

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
uses, in three passes.

**1. Read the board off the company's own careers page** (`discover_from_careers_page`).
Guesses a careers URL from the company name (`careers.<x>.com`, `jobs.<x>.com`,
`<x>.com/careers`), fetches it, and pulls the board's real coordinates out of
the HTML — following a `/jobs`-style link one level in when the landing page is
pure marketing (Snap's board link lives there, not on the front page). This runs
*first* because it's both broader and safer than guessing: it finds boards whose
slug looks nothing like the company (Samsung's Workday tenant is `sec`, Hudson
River Trading's Greenhouse board is `hrttalentcommunity`, US Bank's site is
`US_Bank_Careers`), and a link from the company's own site proves ownership,
which a slug guess does not. Whatever it finds is verified against the live API
before being cached.

**2. Guess slug conventions** for the four single-slug ATSs (e.g. "Capital One"
-> `capitalone`) and verify against the live API.

**3. Probe for Workday coordinates**, which need three parts (tenant +
datacenter + an arbitrary site name) that can't be guessed as one slug: the job
API returns HTTP 404 for a valid tenant+datacenter with a wrong site but 422 for
a wrong tenant/datacenter, which pins the first two cheaply, then common
site-name patterns are tried for the third. For Workday we discover each board's
"Intern" facet (e.g. `workerSubType`) from its own response and filter to intern
roles server-side — much more reliable than a fuzzy `intern` text search, which
buries real intern roles behind experienced ones on big boards.

Watch out for **vendor sandbox boards**: ATSs hand out demo tenants under real
company names, and slug-guessing used to accept them — `lever/linkedin` is
"LinkedIn Partner Sandbox - RSC testing" (23 fake postings) and
`smartrecruiters/uber` holds one posting titled "Test UAT". Those read as
"resolved" while alerting on nothing, which is worse than an unresolved company
because it never shows up in the miss report. `SANDBOX_RE` + `verify_lever` in
`platforms.py` reject them now, and `KNOWN_BAD_BOARDS` in `resolve_companies.py`
evicts any that were already cached.

Beyond the five ATSs, two more board shapes are supported, both auto-detected by
pass 1:

- **`workdaysite`** — Workday's other public host layout,
  `wdN.myworkdaysite.com/recruiting/<tenant>/<site>` (Snap). Same API as
  `workday`, but the datacenter leads the hostname instead of trailing the
  tenant, so it can't be packed into the same slug.
- **`careersite`** — the JSON API some Radancy sites expose at
  `<base>/api/jobs?limit=100&page=N` (GitHub, AMD). Preferred over `jobfeed`
  where both exist, since it carries a structured location and country. `limit`
  silently caps at 100, so it pages. Note GitHub's careers host is on the
  `.careers` TLD rather than a subdomain of `.com`, which is why
  `_careers_urls` guesses that form too.
- **`jobfeed`** — big-employer career sites built on Radancy (Wells Fargo, Uber,
  DraftKings, Caterpillar, Fidelity, Nutanix) are JS-rendered with no JSON API,
  but publish every req as an XML feed at `/jobs/xml/?rss=true`. Two dialects
  exist and a host serves one or the other: RSS `<item>` (title/link only) and
  Radancy's `<source><job>` (adds city/state/country). The feed URL is the slug,
  since there's no tenant id to rebuild it from.

A few big companies aren't on any of those, or post interns somewhere the board
doesn't surface. Those get a **bespoke integration** matched by name in
`CUSTOM_COMPANIES` (in `resolve_companies.py`) rather than auto-resolved:
Amazon (`amazon.jobs` JSON) and Capital One (scrapes their server-rendered
`capitalonecareers.com` and unions in their Workday board, since their tech
interns can appear on either). Adding another is one fetcher in `platforms.py`
plus a name entry; no headless browser, so it stays light.

Results are cached in `state/company_platforms.json`; still-unresolved
companies (custom sites without a known API) are retried automatically after 7
days in case they adopt a supported board later. Each run resolves at most
`MAX_NEW_PER_RUN` new companies so a batch of slow Workday probes can't blow the
hourly job's time budget; the rest are picked up on later runs. Resolution runs
automatically as part of `check_companies.py`, but can also be run standalone to
see the hit/miss breakdown:

```
python resolve_companies.py            # resolve + hit/miss report
python resolve_companies.py --audit    # also fetch every board and report counts
```

`--audit` is the one to run when something feels quiet: it fetches each resolved
board and flags the ones returning `EMPTY` or erroring. A board that resolves but
returns nothing is indistinguishable from a hiring freeze unless you look, so a
dead slug can sit there for months.

Alerts fire only for postings whose title looks like a software-engineering
internship (`intern`/`internship` + a SWE-ish keyword — see `SWE_RE` /
`INTERN_RE` in `check_companies.py` if you want to loosen or tighten that).

### Priority companies (fast lane)

Must-not-miss companies go in `priority.txt` (one per line, same matching as the
watchlist). `check-priority.yml` runs `check_companies.py --priority` **every 15
minutes** — 4x the hourly full check — hitting just those companies' boards
(fetched concurrently), with its own state file
(`state/company_seen_priority.json`) so it never collides with the full run.
Alerts are ⭐-prefixed. This fast lane's extra runs only fit GitHub's free
Actions minutes on a **public** repo (Actions is free/unlimited there); on a
private repo, drop this workflow and let the hourly run cover priority companies
too.

**A `priority.txt` entry only does something if that company resolved to a
board.** The fast lane reads boards directly; a company with no readable board
is silently a no-op here and is only picked up by the 30-min repo watcher, at
whatever lag Simplify has. Run `python resolve_companies.py` and check the
unresolved list before assuming a name in `priority.txt` is being watched
quickly. The stubborn ones are big companies whose careers sites are
JS-rendered with no public API and bot-protection on top (Google, Meta,
Microsoft, Apple, Tesla, Bloomberg, IBM, Walmart, TikTok) — those would each
need a headless browser, which this repo deliberately avoids.

**GitHub Actions does not honour these crons.** Scheduled workflows are
best-effort and get heavily deprioritised: measured over 18h, `check-priority`
(`*/15`) actually fired every ~100 min on average with a worst gap of **3h07m**,
and `check-all` (`7,37`) averaged ~117 min with a worst gap of **3h48m**. So the
"fast lane" is currently no faster than the main check, and overnight both can
go quiet for hours. Nothing in this repo can fix that from inside a cron —
it needs an external trigger (a free uptime pinger hitting the
`workflow_dispatch` API) or a long-running job that loops internally.

## Poke buffer (`buffer_server.py`)

Poke's push can be unreliable, so alerts are also *pullable*: `buffer_server.py`
is a tiny remote MCP server (deployed on Render, see `render.yaml`) exposing one
tool, `get_latest_update`. The checker POSTs each alert to it, and Poke reads it
on demand. Set `BUFFER_URL` (the `/update` endpoint) and `BUFFER_TOKEN`.

The buffer keeps **no durable state of its own** — deliberately. On Render's
free plan the instance sleeps after ~15 min idle and wakes with memory cleared
and its disk reset, which gave every alert a ~15-minute shelf life: one pushed
at 23:15 was gone by ~23:31, so asking Poke an hour later returned "no updates".
The durable copy instead lives in the repo: `check.py` writes
`state/latest_alert.json` (and `state/latest_alert_priority.json` for the fast
lane — separate files so the two workflows never rebase-conflict), the workflow
commits them, and `get_latest_update` falls back to reading those raw URLs,
returning whichever copy has the newest `written_at`. So a sleeping or restarted
buffer no longer loses anything, and `/health` reporting `has_data: false` is
not a problem. Override the feed with `ALERT_FEED_URL` (comma-separated) if the
repo ever moves.

Note this makes alert text publicly readable, since the repo is public.

Two deployment gotchas, both of which present as "Poke can't connect":
- **`fastmcp` must be >= 3.4.5.** In 3.4.4 the `stateless_http` flag is silently
  ignored and every session-less request is rejected with `Bad Request: Missing
  session ID` — which is exactly how connector-style clients call it. Pinned in
  `requirements-buffer.txt`.
- The `LenientMcpEntry` shim fixes two Streamable-HTTP rough edges (an `Accept`
  header match that 406s anything not offering both content types, and a 307 on
  `/mcp/` that clients drop the body on). Don't remove it.

The workflows ping `/health` before running so the alert push doesn't pay a ~50s
cold start. That keeps it warm around *runs*, not around *reads* — if Poke reads
while it's asleep the handshake can still time out, so an external uptime pinger
every ~10 min is worth adding.

## Junior program watcher

`check_programs.py` runs daily (~9:17 AM ET) and alerts when a junior-program
page changes (e.g. applications open): Microsoft Explore, Uber University,
Google STEP, Amazon University/Propel, Jane Street JSIP. Edit `programs.json`
to add/remove pages. Note some sites (Meta, Bloomberg, Citadel) block
automated checks and can't be watched this way.

A page "change" is any edit to the page's visible text, so expect occasional
alerts for cosmetic edits — the message just says which page to go look at.

## Pathway watcher

`check_pathway.py` runs hourly (:22) against [Pathway](https://www.trypathway.app),
a third aggregator alongside the community repos and the direct company boards.
It earns its place by covering companies the other two can't: Pathway lists IBM,
Google, Microsoft and other custom-career-site companies that
`resolve_companies.py` fails to resolve to any board.

Pathway is behind a login and has no API — the full listing set is
server-rendered into the `/internships` document as a Next.js RSC payload. Each
run signs in through Supabase, rebuilds the session cookie the app's server
expects, fetches that one page and parses the job records out of it.

**Requires two secrets**, which are yours to add (`Settings → Secrets and
variables → Actions`):

| secret | value |
|---|---|
| `PATHWAY_EMAIL` | your Pathway account email |
| `PATHWAY_PASSWORD` | your Pathway password |

Signing in per-run is deliberate: a copied cookie or refresh token expires or
rotates within the hour and would need re-pasting, whereas a password grant
mints a fresh token every run and needs no attention.

### The blind-watcher alarm

A watcher's silence is ambiguous — "nothing posted" and "I've been broken for a
week" look identical from the outside. So this one reports itself: after
`ALARM_AFTER` (3) consecutive failed runs it sends a ⚠️ alert through the same
Discord/Poke path as a job alert, and a ✅ when it recovers. `state/pathway_health.json`
holds the counter, and the workflow commits it even on failure so the count
actually accumulates.

Things that trip it: a changed Pathway password, a login/page-structure change,
or Pathway adding MFA. All are loud, none are silent.

## Local test

```
set DISCORD_WEBHOOK_URL=...   (omit for dry-run: messages print to console)
pip install -r requirements.txt
python check.py
python check_companies.py    (first run resolves the whole watchlist -- slow)
set PATHWAY_EMAIL=... & set PATHWAY_PASSWORD=...
python check_pathway.py      (first run seeds ~1,400 listings silently)
```
