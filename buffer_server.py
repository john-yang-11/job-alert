"""Lightweight buffer between the internship checker and Poke.

The checker POSTs its latest alert JSON here; the server keeps the most recent
one in memory and mirrors it to disk. Poke connects to this as a remote MCP
server and reads it via the `get_latest_update` tool — so Poke *pulls* the alert
instead of relying on a push reaching your phone.

Neither of those copies is durable. On an ephemeral free plan (e.g. Render free)
the instance sleeps after ~15 min idle and wakes with memory cleared and disk
reset, which used to give every alert a ~15-minute shelf life: pushed at 23:15,
gone by 23:31, so a read an hour later found nothing. The durable copy is
therefore kept outside this process — the checker writes state/latest_alert.json
and the workflow commits it — and `get_latest_update` falls back to that, so
/health reporting has_data: false no longer means the alert is lost.

Endpoints when running:
  POST /update   -> store latest alert JSON (requires  Authorization: Bearer <BUFFER_TOKEN>)
  GET  /health   -> liveness check (in-process copy only; the feed is the backstop)
  /mcp/          -> MCP (Streamable HTTP); exposes the get_latest_update tool

Env:
  BUFFER_TOKEN    shared secret required on POST /update (if unset, /update is open)
  BUFFER_STATE    path for the on-disk mirror (default: buffer_state.json)
  ALERT_FEED_URL  comma-separated raw URLs of the committed state/latest_alert*.json
                  files, one per lane (defaults to this repo's two)
  PORT            port to listen on (default 8000; hosts like Render set this)

Run locally:  BUFFER_TOKEN=secret python buffer_server.py
Deploy on any host that keeps a process alive (Render/Railway/Fly) so Poke can reach it.
"""

import json
import os
import time
from pathlib import Path

import requests
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

TOKEN = os.environ.get("BUFFER_TOKEN", "")
STATE = Path(os.environ.get("BUFFER_STATE", "buffer_state.json"))
MCP_PATH = "/mcp"

# The durable copies of the last alert: the checker writes state/latest_alert*.json
# and the workflow commits them, so they survive this process dying. Everything
# this server holds itself is lost when a free-tier host sleeps the instance.
# One feed per lane (full check / priority fast lane), because the two workflows
# each commit their own file; whichever is newer wins. Comma-separated to override.
_RAW = "https://raw.githubusercontent.com/john-yang-11/job-alert/main/state"
FEED_URLS = [
    u.strip() for u in os.environ.get(
        "ALERT_FEED_URL",
        f"{_RAW}/latest_alert.json,{_RAW}/latest_alert_priority.json",
    ).split(",") if u.strip()
]
FEED_TTL = float(os.environ.get("ALERT_FEED_TTL", "30"))  # keeps a burst of calls to one fetch
FEED_TIMEOUT = 10

mcp = FastMCP("internship-buffer")


class LenientMcpEntry:
    """ASGI shim that stops well-behaved clients from bouncing off /mcp.

    Two rough edges in the Streamable HTTP layer, both of which show up as a
    failed connection on the client side:

    1. The session manager matches the Accept header by exact substring and
       demands BOTH application/json and text/event-stream. A client sending
       only application/json -- or even a wildcard */* -- gets 406 Not
       Acceptable. We rewrite Accept to the pair the spec wants.
    2. Starlette 307-redirects /mcp/ to /mcp. Clients that drop the body or
       the method on redirect fail mid-handshake, so fold the trailing-slash
       form onto the real path instead of redirecting.

    Only the MCP path is touched; /update and /health negotiate normally.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == MCP_PATH + "/":
                path = MCP_PATH
                scope = dict(scope, path=path, raw_path=path.encode())
            if path == MCP_PATH:
                headers = [(k, v) for k, v in scope["headers"] if k.lower() != b"accept"]
                headers.append((b"accept", b"application/json, text/event-stream"))
                scope = dict(scope, headers=headers)
        await self.app(scope, receive, send)


def _load():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return None


# in-memory buffer, seeded from the disk mirror on startup
_latest = {"data": _load()}
# short-lived cache of the committed copy, so a burst of tool calls is one fetch
_feed_cache = {"at": 0.0, "data": []}


def _fetch_feeds() -> list[dict]:
    """Read the alerts the workflows committed to the repo; [] if unreachable.

    raw.githubusercontent.com serves through a CDN with a ~5 minute TTL, which
    is long enough to hand back the *previous* alert right after a new one is
    committed -- so bust it with a changing query param. A feed 404s until its
    lane has fired at least once, which is normal, not an error.
    """
    now = time.time()
    if now - _feed_cache["at"] < FEED_TTL:
        return _feed_cache["data"]
    found = []
    for url in FEED_URLS:
        try:
            resp = requests.get(
                url,
                params={"t": int(now)},
                headers={"Cache-Control": "no-cache"},
                timeout=FEED_TIMEOUT,
            )
            if resp.ok:
                found.append(resp.json())
        except Exception:
            pass  # unreachable feed just means we fall back to the other copies
    _feed_cache.update(at=now, data=found)
    return found


def _written_at(entry: dict | None) -> str:
    # payloads predating the timestamp sort oldest, which is the safe default
    return (entry or {}).get("written_at", "")


@mcp.custom_route("/update", methods=["POST"])
async def update(request: Request) -> JSONResponse:
    if TOKEN and request.headers.get("authorization") != f"Bearer {TOKEN}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    _latest["data"] = await request.json()
    try:
        STATE.write_text(json.dumps(_latest["data"]), encoding="utf-8")
    except Exception:
        pass  # disk mirror is best-effort; the in-memory copy is the source of truth
    return JSONResponse({"ok": True})


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "has_data": _latest["data"] is not None})


@mcp.tool
def get_latest_update() -> dict:
    """Return the most recent internship alert from the checker script.

    The payload looks like {"content": "<alert text>", "written_at": "<ISO>"}.
    Returns a placeholder when no alert has been recorded yet."""
    # Several copies exist and any of them can be the stale one: the pushed copy
    # is lost whenever this process restarts, each committed copy lags by a run
    # when its push succeeded, and the two lanes commit independently. Whichever
    # was written last is the real answer.
    candidates = [c for c in [_latest["data"], *_fetch_feeds()] if c]
    if not candidates:
        return {"content": "No internship updates yet."}
    return max(candidates, key=_written_at)


if __name__ == "__main__":
    import uvicorn

    # Wrap the app rather than using mcp.run() so the Accept/redirect shim sits
    # in front of the MCP handler. The shim forwards lifespan scopes untouched,
    # which the session manager needs to start.
    #
    # stateless_http: in the default stateful mode the server hands out an
    # mcp-session-id on initialize and rejects every later request that does not
    # echo it back with "400 Bad Request: Missing session ID". That is a bad fit
    # here twice over: connector-style clients often do not carry the id between
    # calls, and sessions live in memory, so a free-plan sleep or restart
    # invalidates a connection that was working a minute ago. This server is one
    # read-only tool over a single value -- there is no session state worth
    # keeping -- so let every request stand on its own.
    uvicorn.run(
        LenientMcpEntry(mcp.http_app(path=MCP_PATH, stateless_http=True)),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
    )
