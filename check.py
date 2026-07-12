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
import re
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

DISCORD_MSG_LIMIT = 2000     # Discord hard limit per message

# local runs: load KEY=VALUE lines from a git-ignored .env (GitHub Actions uses secrets)
_env_file = ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8-sig").splitlines():
        if "=" in _line and not _line.lstrip().startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())


def clean_keyword(raw: str) -> str:
    # drop parenthetical notes like "(JSIP)jane street" -> "jane street"
    return re.sub(r"\([^)]*\)", "", raw).strip()


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
            cell = clean_keyword(row[0])
            if not cell:
                continue
            if i == 0 and any(w in cell.lower() for w in ("company", "keyword", "watchlist", "name")):
                continue
            keywords.append(cell)
        return keywords
    if WATCHLIST_FILE.exists():
        lines = WATCHLIST_FILE.read_text(encoding="utf-8").splitlines()
        cleaned = (clean_keyword(l) for l in lines if l.strip() and not l.strip().startswith("#"))
        return [k for k in cleaned if k]
    return []


def matches(listing: dict, keywords: list[str]) -> bool:
    # whole-word match so "visa" doesn't hit "TelevisaUnivision"
    haystack = f"{listing.get('company_name', '')} {listing.get('title', '')}".lower()
    return any(re.search(rf"\b{re.escape(kw.lower())}\b", haystack) for kw in keywords)


SEASON_RANK = {"summer": 0, "fall": 1, "winter": 2, "spring": 3}


def season(listing: dict) -> tuple[int, str]:
    """Return (sort_rank, display_label) — summer sorts first. Label prefers the
    term with its year (e.g. 'Summer 2026'); falls back to the title's season word."""
    terms = [t for t in (listing.get("terms") or []) if t and t.upper() != "N/A"]
    text = f"{' '.join(terms)} {listing.get('title', '')}".lower()
    for key in ("summer", "fall", "autumn", "winter", "spring"):
        if key in text:
            k = "fall" if key == "autumn" else key
            label = next((t for t in terms if k in t.lower()), k.capitalize())
            return SEASON_RANK[k], label
    return 9, (terms[0] if terms else "")


def format_listing(l: dict) -> str:
    # Discord: company, role as a masked link (no embed), then season
    line = f"**{l.get('company_name', '?')}** — [{l.get('title', '?')}]({l.get('url', '')})"
    label = season(l)[1]
    return f"{line} · {label}" if label else line


def format_listing_plain(l: dict) -> str:
    # Poke (text message): company, role, season — no URL
    line = f"{l.get('company_name', '?')} — {l.get('title', '?')}"
    label = season(l)[1]
    return f"{line} · {label}" if label else line


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
            cut = text.rfind("\n", 0, DISCORD_MSG_LIMIT)
            if cut <= 0:
                cut = DISCORD_MSG_LIMIT
            chunk, text = text[:cut], text[cut:].lstrip("\n")
        # flags=4 (SUPPRESS_EMBEDS) stops Discord from unfurling each link into a preview card
        resp = requests.post(webhook_url, json={"content": chunk, "flags": 4}, timeout=30)
        if not resp.ok:
            print(f"Discord error {resp.status_code}: {resp.text}", file=sys.stderr)
            resp.raise_for_status()


def send_poke(text: str) -> None:
    key = os.environ.get("POKE_API_KEY")
    if not key:
        print("[poke dry-run] would send:\n" + text + "\n" + "-" * 40)
        return
    # The inbound API drops this into your Poke chat "as if you texted it", so we
    # instruct the agent to relay it to your phone verbatim instead of just reading it.
    payload = (
        "Send me a notification with the following internship alert. Relay it "
        "exactly as written — do not summarize, rephrase, or add commentary:\n\n" + text
    )
    # Poke has no documented length limit; iMessage/SMS split long texts on delivery
    resp = requests.post(
        "https://poke.com/api/v1/inbound/api-message",
        headers={"Authorization": f"Bearer {key}"},
        json={"message": payload},
        timeout=30,
    )
    if not resp.ok:
        print(f"Poke error {resp.status_code}: {resp.text}", file=sys.stderr)
        resp.raise_for_status()


def send_buffer(text: str) -> None:
    # push the latest alert to the buffer MCP server so Poke can poll it.
    # best-effort: a buffer outage must not fail the run or block Discord/Poke.
    url = os.environ.get("BUFFER_URL")
    if not url:
        return
    headers = {}
    token = os.environ.get("BUFFER_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        requests.post(url, headers=headers, json={"content": text}, timeout=15)
    except Exception as e:
        print(f"Buffer push failed (ignored): {e}", file=sys.stderr)


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
        msg = f"✅ Internship alert bot is live — watching {n_active} active listings."
        send_discord(msg)
        send_poke(msg)
    else:
        keywords = load_watchlist()
        if not keywords:
            print("WARNING: watchlist is empty — no alerts will be sent", file=sys.stderr)
            matched = []
        else:
            matched = [l for l in new_active if matches(l, keywords)]
        print(f"{len(new_active)} new active listings, {len(matched)} match watchlist")

        if matched:
            matched.sort(key=lambda l: season(l)[0])  # summer first
            plural = "es" if len(matched) != 1 else ""
            header = f"🔔 {len(matched)} new internship match{plural}:"
            plain = header + "\n" + "\n".join(format_listing_plain(l) for l in matched)
            send_discord(header + "\n" + "\n".join(format_listing(l) for l in matched))
            send_poke(plain)
            send_buffer(plain)

    # persist: every id we've now seen (matched or not), plus the new ETag
    seen.update(l["id"] for l in listings if l.get("id"))
    SEEN_FILE.write_text(json.dumps(sorted(seen)))
    etag = resp.headers.get("ETag")
    if etag:
        ETAG_FILE.write_text(etag)
    print("state updated")


if __name__ == "__main__":
    main()
