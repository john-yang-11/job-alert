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

# Community internship repos to watch. Both publish the same listings.json schema
# (id/company_name/title/url/active/is_visible); we read them in one pass and
# dedup across them. Simplify is huge; the CSCareers/vanshb03 repo is smaller and
# partly distinct, so it catches things Simplify misses (and vice versa).
# NOTE: both are the Summer2026 cycle -- bump both URLs to Summer2027 together
# when you switch cycles.
SOURCES = [
    ("Simplify", "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json"),
    ("CSCareers", "https://raw.githubusercontent.com/vanshb03/Summer2026-Internships/dev/.github/scripts/listings.json"),
]

ROOT = Path(__file__).parent
STATE_DIR = ROOT / "state"
SEEN_FILE = STATE_DIR / "seen.json"
ETAGS_FILE = STATE_DIR / "etags.json"   # {source_name: etag}
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


def content_key(listing: dict) -> str:
    """Cross-source dedup key: same real job in both repos gets the same key even
    though each repo assigns it a different id/url. Company + title, normalized."""
    c = re.sub(r"[^a-z0-9]", "", listing.get("company_name", "").lower())
    t = re.sub(r"[^a-z0-9]", "", listing.get("title", "").lower())
    return f"{c}|{t}"


def season(listing: dict) -> tuple[int, str]:
    """Return (sort_rank, display_label) — summer sorts first. Label prefers the
    term with its year (e.g. 'Summer 2026'); falls back to the title's season word."""
    terms = [t for t in (listing.get("terms") or []) if t and t.upper() != "N/A"]
    if not terms and listing.get("season"):   # CSCareers uses a bare season word
        terms = [str(listing["season"])]
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


def load_state() -> tuple[set, set, bool]:
    """Return (seen_ids, seen_keys, needs_reseed). The old on-disk format was a
    bare list of ids; detect it and trigger a one-time silent reseed so adding a
    second source doesn't fire a flood of alerts for jobs we'd otherwise re-see."""
    if not SEEN_FILE.exists():
        return set(), set(), False
    raw = json.loads(SEEN_FILE.read_text())
    if isinstance(raw, list):                      # legacy: ids only, no keys
        return set(raw), set(), True
    return set(raw.get("ids", [])), set(raw.get("keys", [])), False


def save_state(seen_ids: set, seen_keys: set, etags: dict) -> None:
    SEEN_FILE.write_text(json.dumps(
        {"version": 2, "ids": sorted(seen_ids), "keys": sorted(seen_keys)}))
    ETAGS_FILE.write_text(json.dumps(etags))


def fetch_source(name: str, url: str, etag: str | None) -> tuple[list | None, str | None]:
    """Fetch one source's listings. Returns (listings, etag); listings is None on
    a 304 (unchanged). Sends If-None-Match only when we have a prior etag."""
    headers = {"If-None-Match": etag} if etag else {}
    resp = requests.get(url, headers=headers, timeout=120)
    if resp.status_code == 304:
        print(f"{name}: 304 unchanged")
        return None, etag
    resp.raise_for_status()
    data = resp.json()
    print(f"{name}: {len(data)} listings")
    return data, resp.headers.get("ETag") or etag


def main() -> None:
    STATE_DIR.mkdir(exist_ok=True)

    seen_ids, seen_keys, needs_reseed = load_state()
    first_run = not SEEN_FILE.exists()
    seed_only = first_run or needs_reseed   # onboard silently, don't alert this run
    etags = json.loads(ETAGS_FILE.read_text()) if ETAGS_FILE.exists() else {}

    # Fetch every source. On a seeding run, force a full fetch (ignore etags) so we
    # capture all current ids/keys. A failing source is skipped, not fatal.
    fetched: list[dict] = []
    any_changed = False
    for name, url in SOURCES:
        try:
            data, etag = fetch_source(name, url, None if seed_only else etags.get(name))
        except Exception as e:
            print(f"{name}: fetch failed ({e}), skipping", file=sys.stderr)
            continue
        if etag:
            etags[name] = etag
        if data is not None:
            any_changed = True
            fetched.extend(data)

    if not any_changed and not seed_only:
        print("all sources unchanged, nothing to do")
        return

    if seed_only:
        for l in fetched:
            if l.get("id"):
                seen_ids.add(l["id"])
            seen_keys.add(content_key(l))
        n_active = sum(1 for l in fetched if l.get("active"))
        if first_run:
            msg = f"✅ Internship alert bot is live — watching {n_active} active listings across {len(SOURCES)} sources."
        else:
            msg = f"✅ Now also watching the CSCareers repo — {n_active} active listings across {len(SOURCES)} sources."
        send_discord(msg)
        send_poke(msg)
        save_state(seen_ids, seen_keys, etags)
        print("state seeded")
        return

    keywords = load_watchlist()
    matched: list[dict] = []
    if not keywords:
        print("WARNING: watchlist is empty — no alerts will be sent", file=sys.stderr)
    else:
        batch_keys: set = set()
        for l in fetched:
            if l.get("id") in seen_ids:
                continue
            if not (l.get("active") and l.get("is_visible", True)):
                continue
            key = content_key(l)
            if key in seen_keys or key in batch_keys:   # same job from either repo
                continue
            if matches(l, keywords):
                matched.append(l)
                batch_keys.add(key)
    print(f"{len(matched)} new matches across sources")

    if matched:
        matched.sort(key=lambda l: season(l)[0])  # summer first
        plural = "es" if len(matched) != 1 else ""
        header = f"🔔 {len(matched)} new internship match{plural}:"
        plain = header + "\n" + "\n".join(format_listing_plain(l) for l in matched)
        send_discord(header + "\n" + "\n".join(format_listing(l) for l in matched))
        send_poke(plain)
        send_buffer(plain)

    # persist every id and content-key we've now seen (across both sources)
    for l in fetched:
        if l.get("id"):
            seen_ids.add(l["id"])
        seen_keys.add(content_key(l))
    save_state(seen_ids, seen_keys, etags)
    print("state updated")


if __name__ == "__main__":
    main()
