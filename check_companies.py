"""Check watchlist companies' own job boards for new SWE internship postings.

Complements check.py: Simplify's community repo sometimes lags behind a
company's own careers page, or never lists smaller companies at all. For every
watchlist company resolved to a known ATS (see resolve_companies.py), this
pulls current postings straight from that board and alerts on new ones whose
title looks like a software-engineering internship.

State: state/company_seen.json (posting ids already alerted on or seeded).
"""

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from check import (
    STATE_DIR, load_watchlist, send_discord, send_poke, send_all, clean_keyword,
    is_us_location, TARGET_YEAR, ALERT_FILE, PRIORITY_ALERT_FILE,
)
from platforms import PLATFORMS, PlatformThrottled
from resolve_companies import resolve_new, record_board_results, save_cache

# How long a facet-less Workday board's full read stays good, and how many
# boards may be read that way in one run.
#
# The deep read (see _workday_collect) is expensive -- a 2,000-posting board is
# 100 requests -- and reading all 14 facet-less boards at once took 5 minutes and
# turned up 8 intern postings, all on one board, none of them US SWE. That is
# not a per-30-min question. But leaving them on the cheap path meant they were
# silently unwatched, which is the failure this whole module keeps hitting. So
# read them in full on a rotation: every board gets a complete read a few times a
# day, and no single run pays for more than a handful.
DEEP_RESCAN_HOURS = 6
MAX_DEEP_PER_RUN = 4
# Ceiling on how long this run may spend on full reads, whatever the network is
# doing. A local dry run once stretched to two hours when the resolver started
# failing under load; the reads themselves are worth minutes, so cap them and
# let the rotation collect the rest next time.
DEEP_TIME_BUDGET = 240

# Bump whenever is_swe_intern's notion of a match widens. Every posting id ever
# fetched goes into the seen-file, matched or not, so widening the filter is not
# retroactive: a role the previous filter rejected is already marked seen and
# will never alert. That is not a bug in the seen-file -- recording everything is
# what stops a filter change re-alerting an entire board -- but it does mean a
# widening leaves a backlog behind, and last time nobody noticed until three
# "why didn't I get a notification?" investigations later. This makes the run
# say so, and backfill_seen.py collects it.
MATCHER_VERSION = 2

SEEN_FILE = STATE_DIR / "company_seen.json"
PRIORITY_SEEN_FILE = STATE_DIR / "company_seen_priority.json"
MATCHER_FILE = STATE_DIR / "matcher_version.json"
PRIORITY_MATCHER_FILE = STATE_DIR / "matcher_version_priority.json"
PRIORITY_FILE = STATE_DIR.parent / "priority.txt"


def load_priority() -> list[str]:
    """High-priority companies, read only from priority.txt (not the shared
    watchlist sheet), so the fast 10-min run checks just this short list."""
    if not PRIORITY_FILE.exists():
        return []
    lines = PRIORITY_FILE.read_text(encoding="utf-8").splitlines()
    cleaned = (clean_keyword(l) for l in lines if l.strip() and not l.strip().startswith("#"))
    return [k for k in cleaned if k]

SWE_RE = re.compile(
    r"\b(software engineer(ing)?|swe|sde|"
    # Bare "software development" / "software dev" count too. The old form was
    # "software dev(elopment)? engineer", which required the word "engineer" and
    # so missed Intel's "Software Development Graduate Intern" outright.
    r"software dev(eloper|elopment)?|"
    r"full[- ]?stack|back[- ]?end engineer|front[- ]?end engineer|"
    r"site reliability engineer|platform engineer|"
    # Adjacent roles worth hearing about, nearly all at companies already on the
    # watchlist: AMD firmware, TikTok/ByteDance ML, IBM data engineering. Kept
    # tight on purpose -- a bare "embedded" or "data" would drag in the hardware
    # and analytics reqs these boards are full of.
    r"machine learning|ml engineer|ai engineer|applied ai|"
    r"data engineer|firmware|embedded (software|systems|engineer))\b",
    re.IGNORECASE,
)
INTERN_RE = re.compile(r"\bintern(ship)?\b", re.IGNORECASE)


def is_swe_intern(title: str) -> bool:
    return bool(INTERN_RE.search(title) and SWE_RE.search(title))


STALE_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def is_current_cycle(title: str) -> bool:
    """Best-effort year guard for direct company-board titles -- these boards
    have no season metadata like the repo watcher does, just whatever's
    currently open. Drop postings whose title explicitly names a year other
    than TARGET_YEAR; titles with no year mentioned pass through unfiltered
    (a company board only lists currently-open roles anyway, so an untitled
    year is almost always the live cycle)."""
    years = STALE_YEAR_RE.findall(title)
    return not years or TARGET_YEAR in years


def main() -> None:
    priority = "--priority" in sys.argv
    icon = "⭐" if priority else "🏢"
    kind = "Priority" if priority else "Company board"
    seen_file = PRIORITY_SEEN_FILE if priority else SEEN_FILE
    matcher_file = PRIORITY_MATCHER_FILE if priority else MATCHER_FILE
    alert_file = PRIORITY_ALERT_FILE if priority else ALERT_FILE
    STATE_DIR.mkdir(exist_ok=True)

    if priority:
        companies = load_priority()
        if not companies:
            print("priority.txt is empty — nothing to check")
            return
    else:
        companies = load_watchlist()

    cache, names = resolve_new(companies)
    resolved = [(name, cache[name]) for name in names if cache.get(name, {}).get("platform")]
    print(f"{len(resolved)}/{len(names)} companies on a known job board")

    # Say which ones aren't covered. The count alone reads as fine -- "21/31" --
    # while hiding that Google, Meta and Microsoft are in the missing ten. On the
    # priority lane an unwatched company defeats the entire point of the file, so
    # name them loudly there.
    unresolved = [n for n in names if not cache.get(n, {}).get("platform")]
    if unresolved:
        msg = f"{len(unresolved)} with no board, not checked: {', '.join(sorted(unresolved))}"
        if priority:
            print(f"WARNING: {msg}", file=sys.stderr)
        else:
            print(msg)

    # Two watchlist names can point at one board (xfinity and comcast both
    # resolve to workday/comcast|wd5|Comcast_Careers), which costs a duplicate
    # fetch and prints the same error twice. dedup_names() only catches names
    # that differ by casing, so collapse on the resolved board itself.
    by_board: dict[tuple, tuple[str, dict]] = {}
    aliases: dict[str, list[str]] = {}
    for name, info in resolved:
        key = (info["platform"], info["slug"])
        rep, _ = by_board.setdefault(key, (name, info))
        aliases.setdefault(rep, []).append(name)
    if len(by_board) < len(resolved):
        print(f"{len(resolved) - len(by_board)} duplicate board(s) collapsed")
    resolved = list(by_board.values())

    first_run = not seen_file.exists()
    seen: set[str] = set() if first_run else set(json.loads(seen_file.read_text()))

    # How long since the last run of this lane. GitHub schedules cron on a
    # best-effort basis and throttles high-frequency workflows hard: the priority
    # lane asks for every 15 minutes and over 40 consecutive runs got a median
    # gap of 43 minutes and a worst case of 111. Printing the real gap means the
    # drift shows up in the log instead of being a thing only the cron line
    # claims. Read before the file is rewritten below, obviously.
    since = ""
    if not first_run:
        age_min = (datetime.now().timestamp() - seen_file.stat().st_mtime) / 60
        since = f", {age_min:.0f} min since last run"

    # Board fetches are I/O-bound and hit different hosts, so run them
    # concurrently -- turns a ~3-min sequential sweep of ~77 boards into ~30s.

    # Boards whose turn it is for a full read this run, oldest first, so one that
    # has never had a full read goes first and the rotation self-levels. The
    # priority lane never does one -- it needs to stay quick. visa and US bank
    # are on priority.txt and do have facet-less boards, so the fast lane stays
    # blind to them; it says so in a WARNING each run, and the 30-min lane's
    # rotation is what actually covers them. Facet boards are included and cost
    # nothing extra, since
    # _workday_collect ignores `deep` when a facet exists; that keeps this from
    # having to know which boards are facet-less before it has asked them.
    deep_names: set[str] = set()
    if not priority:
        now = datetime.now(timezone.utc)
        due = []
        for name, info in resolved:
            if info["platform"] not in ("workday", "workdaysite"):
                continue
            ts = info.get("deep_scanned_at")
            age = timedelta.max if not ts else now - datetime.fromisoformat(ts)
            if age > timedelta(hours=DEEP_RESCAN_HOURS):
                due.append((age, name))
        due.sort(key=lambda t: t[0], reverse=True)
        deep_names = {n for _, n in due[:MAX_DEEP_PER_RUN]}
        if due:
            print(f"{len(due)} Workday board(s) due a full read; "
                  f"doing {len(deep_names)} this run")

    deep_deadline = time.monotonic() + DEEP_TIME_BUDGET

    def _fetch(name: str, info: dict) -> tuple[str, list[dict]]:
        fetch = PLATFORMS[info["platform"]]
        if name in deep_names:
            return name, fetch(info["slug"], name, deep=True, deadline=deep_deadline)
        return name, fetch(info["slug"], name)

    matched: list[tuple[str, dict]] = []
    errors = []
    all_ids: set[str] = set()
    ok_boards: set[str] = set()
    failed_boards: set[str] = set()
    empty_boards: set[str] = set()
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(_fetch, name, info): name for name, info in resolved}
        for fut in as_completed(futures):
            try:
                name, jobs = fut.result()
            except PlatformThrottled as e:
                # Throttling says nothing about whether the board is alive, so
                # don't let it count toward dropping the entry.
                errors.append(f"{futures[fut]}: {e}")
                continue
            except Exception as e:
                errors.append(f"{futures[fut]}: {e}")
                failed_boards.add(futures[fut])
                continue
            ok_boards.add(name)
            if not jobs:
                # 200 with nothing in it. Not an error, so nothing above catches
                # it, but a board that answers with an empty list is watching
                # exactly as much as a board that 404s.
                empty_boards.add(name)
            for j in jobs:
                all_ids.add(j["id"])
                if j["id"] in seen:
                    continue
                if (
                    is_swe_intern(j["title"])
                    and is_current_cycle(j["title"])
                    and is_us_location([j.get("location", "")], j.get("country", ""))
                ):
                    matched.append((name, j))

    # Stamp only the boards that actually came back, so a throttled or failed
    # deep read is retried next run rather than counting as done for six hours.
    stamp = datetime.now(timezone.utc).isoformat()
    for name in deep_names & ok_boards:
        cache[name]["deep_scanned_at"] = stamp

    # Record before alerting: send_all raises if a sink is down, and a dead board
    # should still be counted on a run where Discord happened to be flaky.
    # Aliases share one fetch, so they share its verdict too -- otherwise the
    # deduped-away name would keep its dead slug forever, never being fetched.
    def _expand(reps: set[str]) -> set[str]:
        return {n for rep in reps for n in aliases.get(rep, [rep])}

    record_board_results(
        cache,
        _expand(ok_boards - empty_boards),
        _expand(failed_boards),
        _expand(empty_boards),
    )
    save_cache(cache)

    if first_run:
        msg = f"{icon} {kind} watcher is live — tracking {len(resolved)} companies directly on their job boards."
        send_discord(msg)
        send_poke(msg)
    elif matched:
        d_lines = "\n".join(f"**{name}** — [{j['title']}]({j['url']})" for name, j in matched)
        p_lines = "\n".join(f"{name} — {j['title']}" for name, j in matched)
        plural = "s" if len(matched) != 1 else ""
        header = f"{icon} {len(matched)} new {kind.lower()} SWE listing{plural}:"
        send_all(f"{header}\n{d_lines}", f"{header}\n{p_lines}", alert_file=alert_file)

    for err in errors:
        print(f"WARNING: {err}", file=sys.stderr)

    # Announce a widened filter rather than letting its backlog sit silent.
    # Skipped on a first run, where there is no backlog by definition.
    recorded = json.loads(matcher_file.read_text()).get("matcher_version", 1)         if matcher_file.exists() else (MATCHER_VERSION if first_run else 1)
    if recorded != MATCHER_VERSION:
        print(f"WARNING: title filter changed (v{recorded} -> v{MATCHER_VERSION}); "
              f"roles the old one rejected are already marked seen and will not "
              f"alert on their own. Run backfill_seen.py to catch them up.",
              file=sys.stderr)
    matcher_file.write_text(json.dumps({"matcher_version": MATCHER_VERSION}))

    seen |= all_ids
    seen_file.write_text(json.dumps(sorted(seen)))
    if empty_boards:
        print(f"WARNING: {len(empty_boards)} board(s) returned no postings at all: "
              f"{', '.join(sorted(empty_boards))}", file=sys.stderr)
    print(
        f"{len(all_ids)} postings checked across {len(resolved)} companies, "
        f"{len(matched)} new SWE-intern match{'es' if len(matched) != 1 else ''}, "
        f"{len(errors)} errors, {len(empty_boards)} empty{since}"
    )


if __name__ == "__main__":
    main()
