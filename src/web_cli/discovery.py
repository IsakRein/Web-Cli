"""Find the running debug Chrome and pick a target (tab) to drive.

Primary path: the CDP HTTP endpoints (`/json/version`, `/json`) exposed on
127.0.0.1:9222 by the always-on debug Chrome. Fallback: read the browser
websocket path from the profile's `DevToolsActivePort` file (line 2) if the HTTP
endpoints are ever unavailable, and drive targets through the browser socket.
"""

import json
import os
import urllib.error
import urllib.request

from .output import die

HOST = "127.0.0.1"
PORT = 9222
BASE = f"http://{HOST}:{PORT}"
PORTFILE = os.path.expanduser(
    "~/Library/Application Support/Google/Chrome-Debug/DevToolsActivePort"
)

MODE_HTTP = "http"
MODE_BROWSER_WS = "browser_ws"

_UNREACHABLE = (
    f"Chrome debug endpoint not reachable at {HOST}:{PORT}. "
    "Is the debug Chrome running? (relaunch /Applications/Chrome.app)"
)


class BrowserConn:
    """How to talk to Chrome: either HTTP discovery or a browser-level websocket."""

    def __init__(self, mode, browser_ws=None):
        self.mode = mode
        self.browser_ws = browser_ws


def _http_get(path, timeout=1.5):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_req(path, method="GET", timeout=5):
    """Hit a /json control endpoint; return parsed JSON when the body is JSON."""
    req = urllib.request.Request(BASE + path, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    try:
        return json.loads(body)
    except ValueError:
        return body


def _browser_ws_url(conn):
    """The browser-level websocket URL, discovered over HTTP when not already known."""
    if conn.mode == MODE_BROWSER_WS:
        return conn.browser_ws
    try:
        return _http_get("/json/version")["webSocketDebuggerUrl"]
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        die(_UNREACHABLE, code=3)


async def new_target(conn, url, background=True):
    """Open a new tab at `url` and return its normalized target dict.

    Goes through Target.createTarget rather than the /json/new HTTP endpoint
    because only createTarget can open the tab in the background — /json/new
    always focuses it, which would interrupt whoever is using the browser.
    """
    from .cdp import CDPSession

    async with CDPSession.connect(_browser_ws_url(conn)) as session:
        res = await session.send(
            "Target.createTarget", {"url": url, "background": background}
        )
    tid = res["targetId"]
    for t in await list_targets(conn):
        if t["id"] == tid:
            return t
    return {"id": tid, "title": "", "url": url, "webSocketDebuggerUrl": None}


async def close_target(conn, target_id):
    """Close the tab with the given target id."""
    if conn.mode == MODE_HTTP:
        _http_req(f"/json/close/{target_id}")
        return

    from .cdp import CDPSession

    async with CDPSession.connect(conn.browser_ws) as session:
        await session.send("Target.closeTarget", {"targetId": target_id})


def find_browser():
    """Resolve how to reach Chrome. HTTP first, DevToolsActivePort file as fallback."""
    try:
        _http_get("/json/version")
        return BrowserConn(MODE_HTTP)
    except (urllib.error.URLError, OSError, ValueError):
        pass

    try:
        with open(PORTFILE, "r") as f:
            lines = f.read().splitlines()
        port = int(lines[0].strip())
        path = lines[1].strip()  # e.g. /devtools/browser/<uuid>
        return BrowserConn(MODE_BROWSER_WS, browser_ws=f"ws://{HOST}:{port}{path}")
    except (OSError, IndexError, ValueError):
        die(_UNREACHABLE, code=3)


async def list_targets(conn):
    """Return page targets as a list of dicts: id, title, url, webSocketDebuggerUrl."""
    if conn.mode == MODE_HTTP:
        try:
            raw = _http_get("/json")
        except (urllib.error.URLError, OSError, ValueError):
            die(_UNREACHABLE, code=3)
        return [_norm(t) for t in raw if t.get("type") == "page"]

    # BROWSER_WS: enumerate via the browser session.
    from .cdp import CDPSession

    async with CDPSession.connect(conn.browser_ws) as session:
        res = await session.send("Target.getTargets")
        out = []
        for t in res.get("targetInfos", []):
            if t.get("type") == "page":
                out.append(
                    {
                        "id": t["targetId"],
                        "title": t.get("title", ""),
                        "url": t.get("url", ""),
                        "webSocketDebuggerUrl": None,  # attach via Target.attachToTarget
                    }
                )
        return out


def _norm(t):
    return {
        "id": t.get("id"),
        "title": t.get("title", ""),
        "url": t.get("url", ""),
        "webSocketDebuggerUrl": t.get("webSocketDebuggerUrl"),
    }


def select_target(targets, target_id=None, url=None):
    """Pick one target by id (exact or unique prefix), url substring, or default first."""
    if not targets:
        die("No page targets open. Open a tab in Chrome first.", code=4)

    if target_id:
        exact = [t for t in targets if t["id"] == target_id]
        if exact:
            return exact[0]
        pref = [t for t in targets if t["id"].startswith(target_id)]
        if len(pref) == 1:
            return pref[0]
        if len(pref) > 1:
            die(f"Target id prefix '{target_id}' is ambiguous.", code=4)
        die(f"No target with id '{target_id}'. Run 'web-cli tabs'.", code=4)

    if url:
        needle = url.lower()
        for t in targets:
            if needle in (t["url"] or "").lower():
                return t
        die(f"No page target whose URL contains '{url}'. Run 'web-cli tabs'.", code=4)

    # Default: first page target (the /json list is MRU-ordered ~= active tab).
    return targets[0]
