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

from check import STATE_DIR, load_watchlist, send_discord, send_poke, send_buffer
from platforms import PLATFORMS
from resolve_companies import resolve_new

SEEN_FILE = STATE_DIR / "company_seen.json"

SWE_RE = re.compile(
    r"\b(software engineer(ing)?|swe|sde|software developer|"
    r"software dev(elopment)? engineer|"  # Amazon's "Software Dev Engineer" / SDE
    r"full[- ]?stack|back[- ]?end engineer|front[- ]?end engineer|"
    r"site reliability engineer|platform engineer)\b",
    re.IGNORECASE,
)
INTERN_RE = re.compile(r"\bintern(ship)?\b", re.IGNORECASE)


def is_swe_intern(title: str) -> bool:
    return bool(INTERN_RE.search(title) and SWE_RE.search(title))


def main() -> None:
    STATE_DIR.mkdir(exist_ok=True)

    cache, names = resolve_new(load_watchlist())
    resolved = [(name, cache[name]) for name in names if cache.get(name, {}).get("platform")]
    print(f"{len(resolved)}/{len(names)} watchlist companies on a known job board")

    first_run = not SEEN_FILE.exists()
    seen: set[str] = set() if first_run else set(json.loads(SEEN_FILE.read_text()))

    matched: list[tuple[str, dict]] = []
    errors = []
    all_ids: set[str] = set()
    for name, info in resolved:
        fetch = PLATFORMS[info["platform"]]
        try:
            jobs = fetch(info["slug"], name)
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue
        for j in jobs:
            all_ids.add(j["id"])
            if j["id"] in seen:
                continue
            if is_swe_intern(j["title"]):
                matched.append((name, j))

    if first_run:
        msg = f"🏢 Company board watcher is live — tracking {len(resolved)} companies directly on their job boards."
        send_discord(msg)
        send_poke(msg)
    elif matched:
        d_lines = "\n".join(f"**{name}** — [{j['title']}]({j['url']})" for name, j in matched)
        p_lines = "\n".join(f"{name} — {j['title']}" for name, j in matched)
        plural = "s" if len(matched) != 1 else ""
        header = f"🏢 {len(matched)} new company-board SWE listing{plural}:"
        send_discord(f"{header}\n{d_lines}")
        send_poke(f"{header}\n{p_lines}")
        send_buffer(f"{header}\n{p_lines}")

    for err in errors:
        print(f"WARNING: {err}", file=sys.stderr)

    seen |= all_ids
    SEEN_FILE.write_text(json.dumps(sorted(seen)))
    print(
        f"{len(all_ids)} postings checked across {len(resolved)} companies, "
        f"{len(matched)} new SWE-intern match{'es' if len(matched) != 1 else ''}, "
        f"{len(errors)} errors"
    )


if __name__ == "__main__":
    main()
