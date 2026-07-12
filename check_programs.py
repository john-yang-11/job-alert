"""Daily watcher for junior/early-career program pages (Microsoft Explore, Google STEP, ...).

Fetches each page in programs.json, hashes its visible text, and sends a Discord
alert when a page's content changes (e.g. applications open). State lives in
state/programs_state.json and is committed back by the workflow.

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


def page_fingerprint(raw_html: str) -> str:
    # visible text only: scripts/styles carry build hashes that change every deploy
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", raw_html)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
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
            fp = page_fingerprint(resp.text)
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
