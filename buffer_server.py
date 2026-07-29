"""Lightweight buffer between the internship checker and Poke.

The checker POSTs its latest alert JSON here; the server keeps the most recent
one in memory and mirrors it to disk. Poke connects to this as a remote MCP
server and reads it via the `get_latest_update` tool — so Poke *pulls* the alert
instead of relying on a push reaching your phone.

Note: the disk mirror only survives a restart on a host with a persistent disk.
On an ephemeral free plan (e.g. Render free) the file is wiped whenever the
instance restarts or wakes from sleep, so /health will report has_data: false
until the next alert is pushed. That is expected, not a failure.

Endpoints when running:
  POST /update   -> store latest alert JSON (requires  Authorization: Bearer <BUFFER_TOKEN>)
  GET  /health   -> liveness check
  /mcp/          -> MCP (Streamable HTTP); exposes the get_latest_update tool

Env:
  BUFFER_TOKEN   shared secret required on POST /update (if unset, /update is open)
  BUFFER_STATE   path for the on-disk mirror (default: buffer_state.json)
  PORT           port to listen on (default 8000; hosts like Render set this)

Run locally:  BUFFER_TOKEN=secret python buffer_server.py
Deploy on any host that keeps a process alive (Render/Railway/Fly) so Poke can reach it.
"""

import json
import os
from pathlib import Path

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

TOKEN = os.environ.get("BUFFER_TOKEN", "")
STATE = Path(os.environ.get("BUFFER_STATE", "buffer_state.json"))
MCP_PATH = "/mcp"

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
    """Return the most recent internship alert pushed by the checker script.

    The payload looks like {"content": "<alert text>"}. Returns a placeholder
    when nothing has been pushed yet."""
    return _latest["data"] or {"content": "No internship updates yet."}


if __name__ == "__main__":
    import uvicorn

    # Wrap the app rather than using mcp.run() so the Accept/redirect shim sits
    # in front of the MCP handler. The shim forwards lifespan scopes untouched,
    # which the session manager needs to start.
    uvicorn.run(
        LenientMcpEntry(mcp.http_app(path=MCP_PATH)),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
    )
