"""Resolve each watchlist company to a known ATS job-board slug.

Simplify's community repo sometimes lags behind a company's own careers page.
For companies on a standard ATS (Greenhouse/Lever/Ashby/SmartRecruiters), we can
skip Simplify entirely and read the job board directly. This script guesses
common slug conventions for each watchlist company (from the same Google Sheet
check.py uses) and verifies each guess against the live API.

Resolution is cached in state/company_platforms.json, keyed by company name,
each entry {"platform": ..., "slug": ..., "checked_at": "<ISO timestamp>"} (or
platform/slug null if unresolved). A resolved entry is trusted forever. An
unresolved one is retried automatically after RETRY_AFTER_DAYS, since a company
may adopt one of these ATS platforms after we first checked -- companies that
never resolve are almost always on a custom career site or Workday (no generic
public API); check_companies.py just skips those.

Run standalone to (re)build the cache and print a hit/miss report:
    python resolve_companies.py
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone

from check import STATE_DIR, load_watchlist
from platforms import (
    ATS_ORDER, PLATFORMS, PlatformError, PlatformThrottled, VERIFIERS, discover_workday,
)

PLATFORMS_FILE = STATE_DIR / "company_platforms.json"
RETRY_AFTER_DAYS = 7

# Companies on fully-custom career sites (no standard board) get a bespoke
# fetcher in platforms.py, matched here by exact normalized name rather than by
# slug-guessing. Keyed by lowercased, non-alnum-stripped company name -> platform.
# Exact-match only, so program rows like "AWS FALL" aren't swept in.
CUSTOM_COMPANIES = {
    "amazon": "amazon",
    "aws": "amazon",
}
# Cap newly-resolved companies per run so a batch of Workday probes (each a slow
# multi-request brute-force) can't blow the hourly job's time budget. Anything
# skipped stays out of the cache and is retried next run. Generous because the
# cache is normally warm -- this only bites on a big watchlist expansion.
MAX_NEW_PER_RUN = 25


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
    for slug in slug_candidates(name):
        for platform in ATS_ORDER:
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
    if PLATFORMS_FILE.exists():
        return json.loads(PLATFORMS_FILE.read_text())
    return {}


def dedup_names(companies: list[str]) -> list[str]:
    # case-insensitive de-dup (the sheet has some casing-only duplicates like
    # "boeing" / "Boeing"); keeps the first-seen display casing.
    seen = {}
    for name in companies:
        seen.setdefault(name.lower(), name)
    return list(seen.values())


def _stale(entry: dict) -> bool:
    if entry.get("platform"):
        return False
    checked_at = entry.get("checked_at")
    if not checked_at:
        return True
    age = datetime.now(timezone.utc) - datetime.fromisoformat(checked_at)
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
    for name in to_resolve:
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
    STATE_DIR.mkdir(exist_ok=True)
    PLATFORMS_FILE.write_text(json.dumps(cache, indent=1, sort_keys=True))
    return cache, names


if __name__ == "__main__":
    cache, names = resolve_new()
    resolved = {k: v for k, v in cache.items() if k in names and v.get("platform")}
    print(f"\n{len(resolved)}/{len(names)} watchlist companies resolved to a job board")
    unresolved = sorted(k for k in names if not cache.get(k, {}).get("platform"))
    if unresolved:
        print("Unresolved (custom career site / Workday — needs a different approach):")
        for name in unresolved:
            print(f"  - {name}")
