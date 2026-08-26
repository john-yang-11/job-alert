"""Resolve each watchlist company to a known ATS job-board slug.

Simplify's community repo sometimes lags behind a company's own careers page.
For companies on a readable job board we can skip Simplify entirely and read the
board directly. For each watchlist company (from the same Google Sheet check.py
uses) this tries, in order:

  1. a bespoke fetcher, for the handful of custom sites worth hand-coding
     (CUSTOM_COMPANIES);
  2. the company's own careers page, reading the board's real coordinates out of
     the HTML (discover_from_careers_page) -- broader and safer than guessing;
  3. common slug conventions on the single-slug ATSs, verified against the live
     API;
  4. a Workday coordinate brute-force, since those can't be guessed as one slug.

Resolution is cached in state/company_platforms.json, keyed by company name,
each entry {"platform": ..., "slug": ..., "checked_at": "<ISO timestamp>"} (or
platform/slug null if unresolved). A resolved entry is trusted forever, except
for boards listed in KNOWN_BAD_BOARDS. An unresolved one is retried
automatically after RETRY_AFTER_DAYS, since a company may move onto a readable
board later -- companies that never resolve are on a custom career site with no
feed; check_companies.py skips those and they stay covered by check.py's repo
watcher alone.

Run standalone to (re)build the cache and print a hit/miss report:
    python resolve_companies.py
    python resolve_companies.py --audit   # + fetch each board, flag dead ones
"""

import json
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from check import STATE_DIR, load_watchlist
from platforms import (
    ATS_ORDER, PLATFORMS, PlatformError, PlatformThrottled, VERIFIERS,
    discover_from_careers_page, discover_workday, verify_discovered,
)

PLATFORMS_FILE = STATE_DIR / "company_platforms.json"
RETRY_AFTER_DAYS = 7
# Consecutive failed board fetches before a resolved entry is dropped and
# re-resolved. Six is ~3h on the 30-min lane: long enough to ride out a board's
# bad afternoon, short enough that a dead slug doesn't sit there for a month.
DROP_AFTER_FAILURES = 6
# How long a *resolved* entry is trusted before being re-checked. Long, because
# a board that works today almost certainly works tomorrow and re-resolving is
# the expensive path; short enough that a company moving off its board is caught
# in weeks rather than never.
RECHECK_HIT_AFTER_DAYS = 30

# Companies on fully-custom career sites (no standard board) get a bespoke
# fetcher in platforms.py, matched here by exact normalized name rather than by
# slug-guessing. Keyed by lowercased, non-alnum-stripped company name -> platform.
# Exact-match only, so program rows like "AWS FALL" aren't swept in.
CUSTOM_COMPANIES = {
    "amazon": "amazon",
    "aws": "amazon",
    "capitalone": "capitalone",
}
# Cap newly-resolved companies per run so a batch of Workday probes (each a slow
# multi-request brute-force) can't blow the hourly job's time budget. Anything
# skipped stays out of the cache and is retried next run. Generous because the
# cache is normally warm -- this only bites on a big watchlist expansion.
MAX_NEW_PER_RUN = 25
# Belt to MAX_NEW_PER_RUN's braces. That cap bounds how many companies we try,
# but not how long each takes, and an unresolvable one is the expensive case:
# careers-page discovery, then 16 slug guesses, then the Workday brute-force,
# all before concluding "no board". A batch of those can outrun the 30-min
# workflow interval, so stop *starting* new ones once the budget is spent and
# let the next run continue (nothing is lost -- unresolved names stay uncached).
RESOLVE_TIME_BUDGET = 300  # seconds

# Boards confirmed to be vendor sandboxes rather than the real company's, so a
# stale cache entry pointing at one is dropped and re-resolved (and never
# re-accepted -- see platforms.SANDBOX_RE / verify_lever). Each of these used to
# read as "resolved" while alerting on nothing.
KNOWN_BAD_BOARDS = {
    ("lever", "linkedin"),          # "LinkedIn Partner Sandbox - RSC testing"
    ("smartrecruiters", "uber"),    # single posting titled "Test UAT"
    ("smartrecruiters", "visa"),    # 2 stub postings; real board is Workday
}


def slug_candidates(name: str) -> list[str]:
    raw = name.strip().lower()
    nospace = re.sub(r"\s+", "", raw)
    alnum = re.sub(r"[^a-z0-9 ]", "", raw)
    words = alnum.split()
    candidates = [nospace, re.sub(r"[^a-z0-9-]", "", nospace)]
    if words:
        candidates += ["".join(words), "-".join(words), "_".join(words)]
    seen, out = set(), []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def resolve_one(name: str) -> dict | None:
    # Bespoke custom-site companies are matched by name and take priority -- this
    # also skips the slow Workday brute-force (and its throttling) for them.
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    if key in CUSTOM_COMPANIES:
        return {"platform": CUSTOM_COMPANIES[key], "slug": key}
    # Read the board off the company's own careers page before guessing slugs.
    # It's both broader (finds boards whose slug looks nothing like the company
    # name) and safer (a slug guess can land on a vendor sandbox), so it wins.
    try:
        found = discover_from_careers_page(name)
    except Exception as e:
        print(f"  {name}: careers-page discovery errored ({e}), skipping", file=sys.stderr)
        found = None
    if found:
        platform, slug = found
        try:
            verify_discovered(platform, slug, name)   # confirm it's live before caching
            return {"platform": platform, "slug": slug}
        except PlatformError as e:
            print(f"  {name}: discovered {platform}/{slug} but it didn't verify ({e})",
                  file=sys.stderr)
        except Exception as e:
            print(f"  {name}: discovered {platform}/{slug} errored ({e})", file=sys.stderr)
    for slug in slug_candidates(name):
        for platform in ATS_ORDER:
            if (platform, slug) in KNOWN_BAD_BOARDS:
                continue
            fetch = PLATFORMS[platform]
            verify = VERIFIERS.get(platform)
            try:
                if verify:
                    if not verify(slug, name):
                        continue
                else:
                    fetch(slug, name)
            except PlatformError:
                continue
            except Exception as e:
                print(f"  {name}: {platform}/{slug} errored ({e}), skipping", file=sys.stderr)
                continue
            return {"platform": platform, "slug": slug}
    # No ATS matched. Workday coordinates aren't a guessable slug, so probe for
    # them separately (two-phase brute-force); catches most of the big companies
    # on custom-looking career sites (Adobe, Salesforce, NVIDIA, Capital One...).
    try:
        wd_slug = discover_workday(name)
    except PlatformThrottled:
        raise  # inconclusive -- let resolve_new leave it uncached to retry
    except Exception as e:
        print(f"  {name}: workday probe errored ({e}), skipping", file=sys.stderr)
        wd_slug = None
    if wd_slug:
        return {"platform": "workday", "slug": wd_slug}
    return None


def load_cache() -> dict:
    if not PLATFORMS_FILE.exists():
        return {}
    cache = json.loads(PLATFORMS_FILE.read_text())
    # A resolved entry is normally trusted forever, so a sandbox board that got
    # cached would never be revisited. Drop those so they re-resolve.
    for name, entry in list(cache.items()):
        if (entry.get("platform"), entry.get("slug")) in KNOWN_BAD_BOARDS:
            print(f"{name}: dropping known-bad board "
                  f"{entry['platform']}/{entry['slug']}, will re-resolve")
            del cache[name]
    return cache


def dedup_names(companies: list[str]) -> list[str]:
    # case-insensitive de-dup (the sheet has some casing-only duplicates like
    # "boeing" / "Boeing"); keeps the first-seen display casing.
    seen = {}
    for name in companies:
        seen.setdefault(name.lower(), name)
    return list(seen.values())


def _stale(entry: dict) -> bool:
    checked_at = entry.get("checked_at")
    if not checked_at:
        return True
    age = datetime.now(timezone.utc) - datetime.fromisoformat(checked_at)
    # A hit expires too, just far more slowly than a miss. Nothing ever
    # invalidated a resolved entry, so a company that moved off its board stayed
    # cached against the dead slug forever -- lever/netflix, lever/atlassian,
    # ashby/tinder and workday/generalmotors have been 404ing on every run,
    # unwatched but still counted as covered. That is the sandbox failure again:
    # a company that looks covered and alerts on nothing. Re-resolving is what
    # notices, so let a hit age out and MAX_NEW_PER_RUN spread the cost (218
    # companies over 30 days is ~7 a day).
    if entry.get("platform"):
        return age > timedelta(days=RECHECK_HIT_AFTER_DAYS)
    return age > timedelta(days=RETRY_AFTER_DAYS)


def resolve_new(companies: list[str] | None = None) -> tuple[dict, list[str]]:
    """Resolve any watchlist company not in the cache, and retry stale misses.

    Returns (cache, deduped_names) so callers don't need to redo the dedup.
    """
    cache = load_cache()
    names = dedup_names(companies if companies is not None else load_watchlist())
    to_resolve = [c for c in names if c not in cache or _stale(cache[c])]
    if len(to_resolve) > MAX_NEW_PER_RUN:
        print(f"{len(to_resolve)} to resolve; capping at {MAX_NEW_PER_RUN} this run "
              f"(rest retried next run)")
        to_resolve = to_resolve[:MAX_NEW_PER_RUN]
    started = time.monotonic()
    for i, name in enumerate(to_resolve):
        if time.monotonic() - started > RESOLVE_TIME_BUDGET:
            print(f"resolution budget ({RESOLVE_TIME_BUDGET}s) spent after {i} companies; "
                  f"{len(to_resolve) - i} deferred to next run")
            break
        try:
            result = resolve_one(name)
        except PlatformThrottled:
            # inconclusive: don't cache a miss, so it's retried next run rather
            # than locked out for RETRY_AFTER_DAYS on a false negative
            print(f"{name}: throttled, leaving uncached to retry next run")
            continue
        entry = (result or {"platform": None, "slug": None})
        entry["checked_at"] = datetime.now(timezone.utc).isoformat()
        cache[name] = entry
        status = f"{result['platform']}/{result['slug']}" if result else "not found"
        print(f"{name}: {status}")
    save_cache(cache)
    return cache, names


def save_cache(cache: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    PLATFORMS_FILE.write_text(json.dumps(cache, indent=1, sort_keys=True))


def record_board_results(cache: dict, ok: set[str], failed: set[str],
                         empty: set[str] = frozenset()) -> None:
    """Feed this run's board outcomes back into the resolution cache.

    _stale() ages a resolved entry out after RECHECK_HIT_AFTER_DAYS, which does
    eventually catch a company that moved off its board -- but "eventually" is a
    month, and the checker already knows within a single run that a board is
    returning nothing. comcast 404'd on every run for a week while still counting
    as covered. So count consecutive failures and drop the entry once it's
    clearly dead, which puts it back in resolve_new()'s queue next run.

    Counting runs rather than matching status codes is deliberate: comcast
    answered HTTP 410, then 422, then non-JSON on successive runs, and
    PlatformError carries only a message string with no status to match on. A
    transient blip -- a read timeout, one bad deploy -- is wiped by the next
    success before it can ever reach the threshold.

    `empty` is counted the same way as `failed`, and is the more important of
    the two. A board that answers HTTP 200 with zero postings raises nothing, so
    error counting alone never sees it -- Qualcomm, aquatic and nutanix have all
    been returning an empty list while counting as covered, and Mastercard sat
    on a board whose intern facet was 19 Latin America roles for ten days
    looking perfectly healthy. A board can of course be legitimately empty
    between seasons; the cost of being wrong is one re-resolution, since
    dropping an entry only sends it back to resolve_new().
    """
    for name in ok:
        cache.get(name, {}).pop("consecutive_failures", None)
    for name in set(failed) | set(empty):
        entry = cache.get(name)
        if not entry:
            continue
        n = entry.get("consecutive_failures", 0) + 1
        if n >= DROP_AFTER_FAILURES:
            why = "empty responses" if name in empty else "board failures"
            print(f"{name}: {n} consecutive {why}, dropping "
                  f"{entry.get('platform')}/{entry.get('slug')} to re-resolve")
            del cache[name]
        else:
            entry["consecutive_failures"] = n


def audit(cache: dict, names: list[str]) -> None:
    """Fetch every resolved board and report what it actually returns.

    A board that resolves but returns nothing looks identical to a quiet hiring
    freeze from the outside, so a broken slug or a sandbox tenant can sit there
    for months without anyone noticing. This makes that visible.
    """
    resolved = [(n, cache[n]) for n in names if cache.get(n, {}).get("platform")]
    print(f"\nauditing {len(resolved)} boards...")
    rows = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(lambda i: PLATFORMS[i[1]["platform"]](i[1]["slug"], i[0]),
                               item): item for item in resolved}
        for fut in as_completed(futures):
            name, info = futures[fut]
            try:
                rows.append((name, info, len(fut.result()), None))
            except Exception as e:
                rows.append((name, info, 0, f"{type(e).__name__}: {str(e)[:60]}"))
    rows.sort(key=lambda r: (r[3] is None, r[2]))
    for name, info, n, err in rows:
        board = f"{info['platform']}/{info['slug']}"
        flag = "ERROR " if err else ("EMPTY " if n == 0 else "      ")
        print(f"  {flag} {name:32s} {board:52s} {err or f'{n} postings'}")
    broken = [r for r in rows if r[3] or r[2] == 0]
    print(f"\n{len(rows) - len(broken)}/{len(rows)} boards returned postings")


if __name__ == "__main__":
    cache, names = resolve_new()
    resolved = {k: v for k, v in cache.items() if k in names and v.get("platform")}
    print(f"\n{len(resolved)}/{len(names)} watchlist companies resolved to a job board")
    by_platform = Counter(v["platform"] for v in resolved.values())
    print("  " + ", ".join(f"{p}: {n}" for p, n in by_platform.most_common()))
    unresolved = sorted(k for k in names if not cache.get(k, {}).get("platform"))
    if unresolved:
        print("Unresolved (custom career site with no readable feed — these stay "
              "covered by the Simplify/CSCareers repo watcher only):")
        for name in unresolved:
            print(f"  - {name}")
    if "--audit" in sys.argv:
        audit(cache, names)
