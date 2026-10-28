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

Profiles: every Chrome profile is its own CDP browser context, but CDP can only
create tabs in the "default" one, which is whichever profile was used last. A
hidden chrome://version target there names that profile's directory; other
contexts are matched by elimination against Local State's open profiles. To
open a tab in another profile, the launcher is run again with
--profile-directory (Chrome hands the URL to the running instance).
"""

import asyncio
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


async def new_target(conn, url, background=True, profile=None):
    """Open a new tab at `url` (in `profile`, if given) and return its normalized target dict.

    Goes through Target.createTarget rather than the /json/new HTTP endpoint
    because only createTarget can open the tab in the background — /json/new
    always focuses it, which would interrupt whoever is using the browser.
    """
    from .cdp import CDPSession

    async with CDPSession.connect(_browser_ws_url(conn)) as session:
        params = {"url": url, "background": background}
        tid = None
        if profile:
            dirs, info, default = await _profiles(session, ())
            want = resolve_profile(info, profile)
            if dirs.get(default) == want:
                params["browserContextId"] = default
            else:
                tid = await _launch_in_profile(session, want, url, background)
        if not tid:
            tid = (await session.send("Target.createTarget", params))["targetId"]
    target = next((t for t in await list_targets(conn) if t["id"] == tid),
                  {"id": tid, "title": "", "url": url, "webSocketDebuggerUrl": None})
    if profile:
        target["profile"] = want
    return target


async def close_target(conn, target_id):
    """Close the tab with the given target id."""
    if conn.mode == MODE_HTTP:
        _http_req(f"/json/close/{target_id}")
        return

    from .cdp import CDPSession

    async with CDPSession.connect(conn.browser_ws) as session:
        await session.send("Target.closeTarget", {"targetId": target_id})


async def _launch_in_profile(session, profile_dir, url, background):
    """Open `url` in a profile CDP cannot reach, by re-running the launcher with --profile-directory.

    Chrome opens it as a foreground tab of that profile's window and comes to
    the front, so the previously frontmost app is handed focus back. The
    profile is then the last-used one, so later opens there stay in CDP.
    """
    if PORT != DEFAULT_PORT or not os.path.exists(APP):
        die(f"Profile '{profile_dir}' is not Chrome's last-used profile, and CDP can only open "
            "tabs in that one. Focus one of its windows once, then retry.", code=4)
    await session.send("Target.setDiscoverTargets", {"discover": True})
    front = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["open", "-g", "-n", "-a", APP, "--args", f"--profile-directory={profile_dir}", url],
                   check=True, capture_output=True)
    try:
        async with asyncio.timeout(LAUNCH_WAIT):
            async for msg in session.events():
                info = msg.get("params", {}).get("targetInfo", {})
                if (msg.get("method") == "Target.targetCreated" and info.get("type") == "page"
                        and info["url"].rstrip("/").lower() == url.rstrip("/").lower()):
                    return info["targetId"]
    except TimeoutError:
        die(f"Ran {APP} for profile '{profile_dir}' but no tab at {url} appeared within "
            f"{LAUNCH_WAIT}s.", code=1)
    finally:
        if background and front:
            await asyncio.sleep(0.5)  # Chrome activates its window just after the tab appears
            subprocess.run(["lsappinfo", "setfront", front], capture_output=True)


async def _probe_profile_path(session, context_id):
    """Profile directory path of a browser context, read off a hidden chrome://version target."""
    tid = (await session.send("Target.createTarget", {
        "url": "chrome://version", "hidden": True, "browserContextId": context_id,
    }))["targetId"]
    try:
        sid = (await session.send("Target.attachToTarget", {"targetId": tid, "flatten": True}))["sessionId"]
        for _ in range(100):
            res = await session.send("Runtime.evaluate", {
                "expression": "document.querySelector('#profile_path')?.innerText",
                "returnByValue": True,
            }, session_id=sid)
            if path := res["result"].get("value"):
                return path.strip()
            await asyncio.sleep(0.02)
        return None
    finally:
        await session.send("Target.closeTarget", {"targetId": tid})


async def _profiles(session, contexts):
    """({context id: profile dir}, Local State profile info_cache, default context id).

    Only the default (last-used) context is probed; of the other `contexts`, a
    single one left over is matched to the single other open profile.
    """
    default = (await session.send("Target.getBrowserContexts"))["defaultBrowserContextId"]
    path = await _probe_profile_path(session, default)
    if not path:
        return {}, {}, default
    root, name = os.path.split(path)
    try:
        with open(os.path.join(root, "Local State")) as f:
            state = json.load(f).get("profile", {})
    except (OSError, ValueError):
        state = {}
    dirs = {default: name}
    rest = set(contexts) - {None, default}
    left = [d for d in state.get("last_active_profiles", []) if d != name]
    # ponytail: elimination labels one other context at most; 3+ open profiles leave the rest unknown
    if len(rest) == 1 and len(left) == 1:
        dirs[rest.pop()] = left[0]
    return dirs, state.get("info_cache", {}), default


def resolve_profile(info, query):
    """Profile directory whose directory, name or email equals `query`, else contains it."""
    q = query.lower()
    keys = {d: [(s or "").lower() for s in (d, p.get("name"), p.get("user_name"))] for d, p in info.items()}
    hits = [d for d, ks in keys.items() if q in ks] or [d for d, ks in keys.items() if any(q in k for k in ks)]
    if len(hits) == 1:
        return hits[0]
    known = ", ".join(f"{d} ({p.get('name')}, {p.get('user_name') or 'no account'})" for d, p in info.items())
    what = "is ambiguous" if hits else "matches no profile"
    die(f"Profile '{query}' {what}. Profiles: {known or 'none found'}.", code=4)


async def tag_profiles(conn, targets):
    """Set each target's "profile" (directory, None when unknown); return Local State's profile info."""
    from .cdp import CDPSession

    async with CDPSession.connect(_browser_ws_url(conn)) as session:
        infos = (await session.send("Target.getTargets"))["targetInfos"]
        ctx = {t["targetId"]: t.get("browserContextId") for t in infos}
        dirs, info, _ = await _profiles(session, {ctx.get(t["id"]) for t in targets})
    for t in targets:
        t["profile"] = dirs.get(ctx.get(t["id"]))
    return info


async def pick_target(conn, args):
    """The target a command's selectors (-t, --url, --profile) name."""
    targets = await list_targets(conn)
    if args.profile and not args.target_id:
        want = resolve_profile(await tag_profiles(conn, targets), args.profile)
        targets = [t for t in targets if t["profile"] == want]
        if not targets:
            die(f"No tabs open in profile '{want}'.", code=4)
    return select_target(targets, args.target_id, args.url)


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
