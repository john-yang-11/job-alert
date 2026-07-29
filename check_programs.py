"""Daily watcher for junior/early-career program pages (Microsoft Explore, Jane Street JSIP, ...).

Fetches each page in programs.json and fingerprints it, then sends a Discord
alert when the fingerprint changes. State lives in state/programs_state.json
and is committed back by the workflow.

A plain full-page text hash fires on any cosmetic edit (nav changes, rotating
testimonials, asset-hash filenames, copyright year, ...), not just the change
we actually care about. So each program has a bespoke EXTRACTOR that scopes
the fingerprint to just the relevant chunk of the page (e.g. Jane Street's
JSIP program card, not the other 11 programs on that page). Programs without
a bespoke extractor fall back to hashing the whole visible-text page, same as
before -- add a new program to programs.json and it just works, add an
extractor here later if it turns out noisy.

Reuses the Discord sender and .env loading from check.py.
"""

import hashlib
import html
import json
import re
import sys

import requests

from check import STATE_DIR, send_discord, send_poke

PROGRAMS_FILE = STATE_DIR.parent / "programs.json"
PROGRAMS_STATE = STATE_DIR / "programs_state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def visible_text(raw_html: str) -> str:
    # visible text only: scripts/styles carry build hashes that change every deploy
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", raw_html)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def extract_ms_explore(raw_html: str) -> str:
    # Job cards are clean and structured: title + post date + location.
    # Scoping to these (instead of the whole page) ignores the FAQ / "Life at
    # Microsoft" blurbs and only fires when a listing is added, removed, or edited.
    card_re = re.compile(
        r'careers-joblistResponsive-subheading">(?P<title>[^<]+)</h3>.*?'
        r'postdate"[^>]*>(?P<date>[^<]*)</div>.*?'
        r'primarylocation"[^>]*>(?P<loc>[^<]*)</div>',
        re.S,
    )
    cards = card_re.findall(raw_html)
    lines = sorted(f"{_clean(t)} | {_clean(d)} | {_clean(l)}" for t, d, l in cards)
    return "\n".join(lines) if lines else "NO_LISTINGS_FOUND"


def extract_jane_street_jsip(raw_html: str) -> str:
    # The programs-and-events page lists 12 programs as separately-classed cards;
    # scope to just the JSIP card's status badge + description so the other 11
    # (and nav/footer/asset-hash filenames) don't cause false-positive alerts.
    block_m = re.search(r'href="/join-jane-street/programs-and-events/jsip/".*?</a>', raw_html, re.S)
    if not block_m:
        return "JSIP_CARD_MISSING"
    block = block_m.group(0)
    status = re.search(r'subheading">([^<]*)</h6>', block)
    desc = re.search(r'class="description">([^<]*)</p>', block)
    return f"{_clean(status.group(1)) if status else '?'} || {_clean(desc.group(1)) if desc else '?'}"


def extract_amazon_sde(raw_html: str) -> str:
    # The page ships a global nav/mega-menu shell (with its own unrelated
    # <footer>) before the real content region starts at id="cms-root".
    # Amazon doesn't publish an explicit open/closed indicator here, so this
    # is coarser than Microsoft/Jane Street -- expect it to catch page-copy
    # edits too, not just application-window changes.
    start = raw_html.find('id="cms-root"')
    end = raw_html.find("<footer", start) if start >= 0 else -1
    region = raw_html[start:end] if start >= 0 and end > start else raw_html
    return visible_text(region)


def extract_uber(raw_html: str) -> str:
    # No per-program status card exists on this page (it's a generic branding
    # page with a rotating testimonial carousel). Scoping to <main>..<footer>
    # at least drops nav/footer noise; same caveat as Amazon above.
    start = raw_html.find("<main")
    end = raw_html.find("<footer", start) if start >= 0 else -1
    region = raw_html[start:end] if start >= 0 and end > start else raw_html
    return visible_text(region)


EXTRACTORS = {
    "https://careers.microsoft.com/v2/global/en/exploremicrosoft": extract_ms_explore,
    "https://www.janestreet.com/join-jane-street/programs-and-events/": extract_jane_street_jsip,
    "https://amazon.jobs/content/en/career-programs/university/sde": extract_amazon_sde,
    "https://jobs.uber.com/en/teams/emerging-talent/": extract_uber,
}


def fingerprint(url: str, raw_html: str) -> str:
    text = EXTRACTORS.get(url, visible_text)(raw_html)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> None:
    STATE_DIR.mkdir(exist_ok=True)
    programs = json.loads(PROGRAMS_FILE.read_text(encoding="utf-8"))
    first_run = not PROGRAMS_STATE.exists()
    state = {} if first_run else json.loads(PROGRAMS_STATE.read_text())
    state = {u: h for u, h in state.items() if u in {p["url"] for p in programs}}

    changed, errors = [], []
    for prog in programs:
        name, url = prog["name"], prog["url"]
        try:
            resp = requests.get(url, headers=HEADERS, timeout=45)
            resp.raise_for_status()
            fp = fingerprint(url, resp.text)
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue
        old = state.get(url)
        if old is not None and old != fp:
            changed.append(prog)
        state[url] = fp

    if first_run:
        msg = (
            f"📋 Program watcher is live — tracking {len(state)} junior-program pages "
            "(daily check for updates like applications opening)."
        )
        send_discord(msg)
        send_poke(msg)
    elif changed:
        d_lines = "\n".join(f"• **{p['name']}** — check <{p['url']}>" for p in changed)
        p_lines = "\n".join(f"• {p['name']}" for p in changed)
        send_discord(f"📋 Program page update detected:\n{d_lines}")
        send_poke(f"📋 Program page update (check the board):\n{p_lines}")

    for err in errors:
        print(f"WARNING: {err}", file=sys.stderr)
    print(f"{len(state)} pages checked, {len(changed)} changed, {len(errors)} errors")
    PROGRAMS_STATE.write_text(json.dumps(state, indent=1))


if __name__ == "__main__":
    main()
