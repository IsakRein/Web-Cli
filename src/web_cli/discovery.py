"""Find the running debug Chrome and pick a target (tab) to drive.

Primary path: the CDP HTTP endpoints (`/json/version`, `/json`) exposed on
127.0.0.1:9222 by the always-on debug Chrome. Fallback: read the browser
websocket path from the profile's `DevToolsActivePort` file (line 2) if the HTTP
endpoints are ever unavailable, and drive targets through the browser socket.
If Chrome isn't running at all, launch /Applications/Chrome.app (without
stealing focus) and wait for the CDP endpoint to come up.

WEB_CLI_PORT points every command at another Chrome's debug port instead (e.g.
a headless instance a script launched itself). That Chrome is the caller's to
start: on a non-default port there is no profile-file fallback and no launch.
"""

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request

from .output import die, err_console

HOST = "127.0.0.1"
DEFAULT_PORT = 9222
PORT = int(os.environ.get("WEB_CLI_PORT") or DEFAULT_PORT)
BASE = f"http://{HOST}:{PORT}"
PORTFILE = os.path.expanduser(
    "~/Library/Application Support/Google/Chrome-Debug/DevToolsActivePort"
)
APP = "/Applications/Chrome.app"
LAUNCH_WAIT = 20  # seconds to wait for CDP after launching the app

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


def _try_connect():
    """One connection attempt: HTTP first, DevToolsActivePort file as fallback."""
    try:
        _http_get("/json/version")
        return BrowserConn(MODE_HTTP)
    except (urllib.error.URLError, OSError, ValueError):
        pass

    if PORT != DEFAULT_PORT:
        return None  # PORTFILE belongs to the default debug Chrome
    try:
        with open(PORTFILE, "r") as f:
            lines = f.read().splitlines()
        port = int(lines[0].strip())
        path = lines[1].strip()  # e.g. /devtools/browser/<uuid>
        # Chrome doesn't always clean this file up on shutdown; only trust it
        # if something is actually listening on the port.
        with socket.create_connection((HOST, port), timeout=1):
            pass
        return BrowserConn(MODE_BROWSER_WS, browser_ws=f"ws://{HOST}:{port}{path}")
    except (OSError, IndexError, ValueError):
        return None


def find_browser():
    """Resolve how to reach Chrome, launching the debug Chrome if it isn't running."""
    conn = _try_connect()
    if conn:
        return conn

    if PORT != DEFAULT_PORT:
        die(f"No Chrome debug endpoint at {HOST}:{PORT} (WEB_CLI_PORT); start that "
            "Chrome first.", code=3)

    if not os.path.exists(APP):
        die(
            f"Chrome debug endpoint not reachable at {HOST}:{PORT} and {APP} "
            "does not exist (build it with ~/.dotfiles/bootstrap.sh).",
            code=3,
        )

    err_console.print(f"[dim]debug Chrome not running; launching {APP} ...[/dim]")
    try:
        # -g keeps the app from stealing focus from whatever the user is doing.
        subprocess.run(["open", "-g", "-a", APP], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as e:
        detail = getattr(e, "stderr", b"") or b""
        die(f"Failed to launch {APP}: {detail.decode().strip() or e}", code=3)

    deadline = time.monotonic() + LAUNCH_WAIT
    while time.monotonic() < deadline:
        try:
            _http_get("/json/version", timeout=1)
            return BrowserConn(MODE_HTTP)
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.25)
    die(
        f"Launched {APP} but the CDP endpoint at {HOST}:{PORT} did not come up "
        f"within {LAUNCH_WAIT}s.",
        code=3,
    )


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
