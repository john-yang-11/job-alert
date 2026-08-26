"""One-shot: alert on roles the old title filter silently dropped.

check_companies.py records *every* posting id it fetches into
state/company_seen.json, matched or not. That is deliberate -- it's what stops a
filter change from re-alerting the whole board -- but it means widening the
filter is not retroactive. A role the old SWE_RE rejected is already marked
seen, so the widened SWE_RE will never surface it, and the only postings that
ever alert are ones first seen after the change.

At the time of writing that stranded 14 currently-open US roles at companies on
the watchlist: AMD's firmware and ML intern reqs, three at Neuralink, Etched's
firmware intern, Point72's ML researcher. Exactly the roles the widening was
for.

This finds them -- open now, matching the current filter, already marked seen,
and rejected by the filter named in PREVIOUS_SWE_RE -- and sends them once
through the normal alert path.

    python backfill_seen.py            # dry run, prints what it would send
    python backfill_seen.py --send     # actually send

Not scheduled, and not meant to be kept around after it has been run: if the
filter is widened again, update PREVIOUS_SWE_RE to whatever is being replaced
and run it again.
"""

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from check import load_watchlist, send_all, is_us_location, ALERT_FILE
from check_companies import (
    SEEN_FILE, is_swe_intern, is_current_cycle, INTERN_RE,
)
from platforms import PLATFORMS
from resolve_companies import load_cache, dedup_names

# The filter this backfill is catching up from -- the one in check_companies.py
# before ML/AI, data engineering, firmware, embedded and bare "software
# development" were added. Anything matching this already had its chance to
# alert, so it is not backfilled.
PREVIOUS_SWE_RE = re.compile(
    r"\b(software engineer(ing)?|swe|sde|software dev(elopment)? engineer|"
    r"software developer|full[- ]?stack|back[- ]?end engineer|"
    r"front[- ]?end engineer|site reliability engineer|platform engineer)\b",
    re.IGNORECASE,
)


def previously_matched(title: str) -> bool:
    return bool(INTERN_RE.search(title) and PREVIOUS_SWE_RE.search(title))


def main() -> None:
    send = "--send" in sys.argv
    if not SEEN_FILE.exists():
        print(f"{SEEN_FILE} does not exist -- nothing has been seen yet, so "
              f"there is no backlog to catch up on.")
        return
    seen = set(json.loads(SEEN_FILE.read_text()))
    print(f"{len(seen)} posting ids already seen")

    # load_cache, not resolve_new: resolving writes the cache back, and a dry
    # run must not change anything. Boards already resolved are all we need.
    cache = load_cache()
    names = dedup_names(load_watchlist())
    boards: dict[tuple, list[str]] = {}
    for name in names:
        info = cache.get(name) or {}
        if info.get("platform"):
            boards.setdefault((info["platform"], info["slug"]), []).append(name)
    print(f"reading {len(boards)} boards")

    def fetch(key: tuple, board_names: list[str]) -> tuple[str, list[dict]]:
        platform, slug = key
        return board_names[0], PLATFORMS[platform](slug, board_names[0])

    stranded: list[tuple[str, dict]] = []
    errors = 0
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(fetch, k, v): v[0] for k, v in boards.items()}
        for fut in as_completed(futures):
            try:
                name, jobs = fut.result()
            except Exception as e:
                print(f"WARNING: {futures[fut]}: {e}", file=sys.stderr)
                errors += 1
                continue
            for j in jobs:
                if j["id"] not in seen:
                    # Not stranded -- the next scheduled run alerts this
                    # normally, and sending it here would only duplicate it.
                    continue
                if previously_matched(j["title"]):
                    continue
                if (
                    is_swe_intern(j["title"])
                    and is_current_cycle(j["title"])
                    and is_us_location([j.get("location", "")], j.get("country", ""))
                ):
                    stranded.append((name, j))

    stranded.sort(key=lambda t: (t[0].lower(), t[1]["title"]))
    print(f"\n{len(stranded)} stranded role(s), {errors} board error(s)\n")
    for name, j in stranded:
        print(f"  {name} — {j['title']}  [{j.get('location', '')}]")
        print(f"      {j['url']}")
    if not stranded:
        return

    header = (f"🗂 {len(stranded)} role{'s' if len(stranded) != 1 else ''} the old "
              f"title filter missed (one-off catch-up, these are already open):")
    discord = "\n".join(f"**{n}** — [{j['title']}]({j['url']})" for n, j in stranded)
    plain = "\n".join(f"{n} — {j['title']}" for n, j in stranded)
    if not send:
        print(f"\n[dry run] re-run with --send to deliver the above. "
              f"Nothing was sent and no state was changed.")
        return
    send_all(f"{header}\n{discord}", f"{header}\n{plain}", alert_file=ALERT_FILE)
    print(f"\nsent {len(stranded)} role(s)")


if __name__ == "__main__":
    main()
