"""Check SimplifyJobs/Summer2026-Internships for new listings and alert via Discord.

Runs every 30 min on GitHub Actions (see .github/workflows/check.yml).
State (seen listing IDs + ETag) lives in state/ and is committed back by the workflow.

Env vars:
  DISCORD_WEBHOOK_URL  channel webhook URL (if missing, runs in dry-run mode)
  WATCHLIST_CSV_URL    optional: published-to-web CSV URL of a Google Sheet;
                       falls back to watchlist.txt next to this script
"""

import csv
import io
import json
import os
import sys
from pathlib import Path

import requests

# Windows consoles default to cp1252, which can't print the emoji in dry-run output
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

LISTINGS_URL = "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json"

ROOT = Path(__file__).parent
STATE_DIR = ROOT / "state"
SEEN_FILE = STATE_DIR / "seen.json"
ETAG_FILE = STATE_DIR / "etag.txt"
WATCHLIST_FILE = ROOT / "watchlist.txt"

BATCH_THRESHOLD = 5          # >5 matches in one run -> single combined message
DISCORD_MSG_LIMIT = 2000     # Discord hard limit per message


def load_watchlist() -> list[str]:
    csv_url = os.environ.get("WATCHLIST_CSV_URL")
    if csv_url:
        resp = requests.get(csv_url, timeout=30)
        resp.raise_for_status()
        rows = list(csv.reader(io.StringIO(resp.text)))
        # first column of each row; skip a header row if it looks like one
        keywords = []
        for i, row in enumerate(rows):
            if not row or not row[0].strip():
                continue
            cell = row[0].strip()
            if i == 0 and cell.lower() in ("company", "companies", "keyword", "keywords", "name"):
                continue
            keywords.append(cell)
        return keywords
    if WATCHLIST_FILE.exists():
        lines = WATCHLIST_FILE.read_text(encoding="utf-8").splitlines()
        return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
    return []


def matches(listing: dict, keywords: list[str]) -> bool:
    haystack = f"{listing.get('company_name', '')} {listing.get('title', '')}".lower()
    return any(kw.lower() in haystack for kw in keywords)


def format_listing(l: dict) -> str:
    locations = ", ".join(l.get("locations") or [])
    # <url> suppresses Discord's link preview embed
    return (
        f"**{l.get('company_name', '?')}** — {l.get('title', '?')}\n"
        f"📍 {locations or 'N/A'}  |  {l.get('category', '')}\n"
        f"Apply: <{l.get('url', '')}>"
    )


def send_discord(text: str) -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("[dry-run] would send:\n" + text + "\n" + "-" * 40)
        return
    # chunk if over Discord's limit, splitting on listing boundaries where possible
    while text:
        if len(text) <= DISCORD_MSG_LIMIT:
            chunk, text = text, ""
        else:
            cut = text.rfind("\n\n", 0, DISCORD_MSG_LIMIT)
            if cut <= 0:
                cut = DISCORD_MSG_LIMIT
            chunk, text = text[:cut], text[cut:].lstrip("\n")
        resp = requests.post(webhook_url, json={"content": chunk}, timeout=30)
        if not resp.ok:
            print(f"Discord error {resp.status_code}: {resp.text}", file=sys.stderr)
            resp.raise_for_status()


def main() -> None:
    STATE_DIR.mkdir(exist_ok=True)

    # cheap change check: skip the 10 MB download if the file hasn't changed
    headers = {}
    if ETAG_FILE.exists() and SEEN_FILE.exists():
        headers["If-None-Match"] = ETAG_FILE.read_text().strip()
    resp = requests.get(LISTINGS_URL, headers=headers, timeout=120)
    if resp.status_code == 304:
        print("304: listings.json unchanged, nothing to do")
        return
    resp.raise_for_status()
    listings = resp.json()
    print(f"downloaded {len(listings)} listings")

    first_run = not SEEN_FILE.exists()
    seen: set[str] = set() if first_run else set(json.loads(SEEN_FILE.read_text()))

    new = [l for l in listings if l.get("id") not in seen]
    new_active = [l for l in new if l.get("active") and l.get("is_visible", True)]

    if first_run:
        n_active = sum(1 for l in listings if l.get("active"))
        send_discord(f"✅ Internship alert bot is live — watching {n_active} active listings.")
    else:
        keywords = load_watchlist()
        if not keywords:
            print("WARNING: watchlist is empty — no alerts will be sent", file=sys.stderr)
            matched = []
        else:
            matched = [l for l in new_active if matches(l, keywords)]
        print(f"{len(new_active)} new active listings, {len(matched)} match watchlist")

        if len(matched) > BATCH_THRESHOLD:
            body = "\n\n".join(format_listing(l) for l in matched)
            send_discord(f"🔔 {len(matched)} new matching internships:\n\n{body}")
        else:
            for l in matched:
                send_discord("🔔 New internship match!\n\n" + format_listing(l))

    # persist: every id we've now seen (matched or not), plus the new ETag
    seen.update(l["id"] for l in listings if l.get("id"))
    SEEN_FILE.write_text(json.dumps(sorted(seen)))
    etag = resp.headers.get("ETag")
    if etag:
        ETAG_FILE.write_text(etag)
    print("state updated")


if __name__ == "__main__":
    main()
