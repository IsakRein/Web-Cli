"""Intercept a tab's network via CDP Fetch and rewrite it with a Python hook.

Attaches to the tab, enables the Fetch domain, and routes every paused request
through a hook file you provide. The hook can answer a request locally (the
server never sees it) or rewrite a real response's body before the page does.
Runs until Ctrl-C or --duration, printing one line per intercepted request.

The point: demo or test a frontend against data the backend doesn't have —
inject a field into an API response, fake a write endpoint so nothing
persists, reproduce a state the real data can't reach.

Hook file contract (a plain Python file, loaded once):

    MATCH = ["substr", ...]      # optional: only URLs containing one of these
                                 # are paused at all; others never leave Chrome

    def on_request(request):     # optional: request stage
        '''request: {"url", "method", "post_data"}.
        Return None to send it to the network, or a dict to answer locally:
        {"status": 200, "body": <dict|str|bytes>, "headers": {...}}.
        A dict body is JSON-encoded with content-type application/json.'''

    def on_response(request, status, body):  # optional: response stage
        '''body: decoded text of the real response.
        Return None to pass it through untouched, or a replacement body
        (dict|str|bytes) to hand the page instead.'''

Module-level state persists across calls, so on_response can stash a GET and
on_request can answer a later PUT from it.
"""

import asyncio
import base64
import json
from datetime import datetime

from rich.text import Text

from ..cdp import CDPError, open_target
from ..discovery import list_targets, select_target
from ..output import console, die, emit_ndjson


async def run(args, conn):
    hook = _load_hook(args.hook)
    target = select_target(await list_targets(conn), args.target_id, args.url)

    if not args.json:
        stop = f"{args.duration}s" if args.duration else "Ctrl-C to stop"
        console.print(
            f"[dim]mocking on[/dim] {target['url']}  [dim]({stop})[/dim]",
            no_wrap=True, overflow="ellipsis", crop=True,
        )

    async with open_target(conn, target) as (session, sid):
        await session.send(
            "Fetch.enable", {"patterns": _patterns(hook)}, session_id=sid
        )
        if args.reload:
            await session.send("Page.enable", session_id=sid)
            await session.send("Page.reload", session_id=sid)

        drain = _drain(session, sid, args, hook)
        try:
            if args.duration:
                async with asyncio.timeout(args.duration):
                    await drain
            else:
                await drain
        except (TimeoutError, asyncio.CancelledError, KeyboardInterrupt):
            pass  # clean stop; Fetch auto-disables when the session closes


def _load_hook(path):
    ns = {}
    try:
        with open(path, encoding="utf-8") as f:
            code = f.read()
    except OSError as e:
        die(f"Could not read hook file: {e}")
    try:
        exec(compile(code, path, "exec"), ns)  # noqa: S102 - user-authored hook
    except Exception as e:
        die(f"Hook file failed to load: {e!r}")
    if not callable(ns.get("on_request")) and not callable(ns.get("on_response")):
        die("Hook file defines neither on_request nor on_response.")
    return ns


def _patterns(hook):
    """Pause only what the hook can act on, at only the stages it handles."""
    urls = hook.get("MATCH") or ["*"]
    patterns = []
    for u in urls:
        pattern = u if u == "*" else f"*{u}*"
        if callable(hook.get("on_request")):
            patterns.append({"urlPattern": pattern, "requestStage": "Request"})
        if callable(hook.get("on_response")):
            patterns.append({"urlPattern": pattern, "requestStage": "Response"})
    return patterns


async def _drain(session, sid, args, hook):
    async for msg in session.events():
        if msg.get("method") != "Fetch.requestPaused":
            continue
        p = msg.get("params", {})
        try:
            await _handle(session, sid, args, hook, p)
        except CDPError:
            # The request may be gone (tab navigated); nothing to release.
            pass


async def _handle(session, sid, args, hook, p):
    rid = p["requestId"]
    req = p.get("request", {})
    request = {
        "url": req.get("url"),
        "method": req.get("method"),
        "post_data": req.get("postData"),
    }
    at_response = p.get("responseStatusCode") is not None or p.get("responseErrorReason")

    if not at_response:
        result = _call(hook, args, "on_request", request)
        if result is None:
            await session.send("Fetch.continueRequest", {"requestId": rid}, session_id=sid)
            return
        body, content_type = _encode_body(result.get("body"))
        headers = {"content-type": content_type, **(result.get("headers") or {})}
        # A locally-answered response bypasses the server, so it carries no
        # CORS headers; without them the browser blocks it for cross-origin
        # API calls. Reflect the request's Origin unless the hook set its own.
        origin = next((v for k, v in req.get("headers", {}).items() if k.lower() == "origin"), None)
        if origin and not any(k.lower() == "access-control-allow-origin" for k in headers):
            headers["access-control-allow-origin"] = origin
            headers["access-control-allow-credentials"] = "true"
        await session.send(
            "Fetch.fulfillRequest",
            {
                "requestId": rid,
                "responseCode": int(result.get("status", 200)),
                "responseHeaders": [{"name": k, "value": str(v)} for k, v in headers.items()],
                "body": base64.b64encode(body).decode("ascii"),
            },
            session_id=sid,
        )
        _emit(args, "mocked", request)
        return

    status = p.get("responseStatusCode")
    raw = await session.send("Fetch.getResponseBody", {"requestId": rid}, session_id=sid)
    text = raw.get("body", "")
    if raw.get("base64Encoded"):
        text = base64.b64decode(text).decode("utf-8", errors="replace")
    replacement = _call(hook, args, "on_response", request, status, text)
    if replacement is None:
        await session.send("Fetch.continueResponse", {"requestId": rid}, session_id=sid)
        return
    body, content_type = _encode_body(replacement)
    headers = {
        h["name"].lower(): h["value"]
        for h in p.get("responseHeaders") or []
        if h["name"].lower() not in ("content-length", "content-encoding", "transfer-encoding")
    }
    headers["content-type"] = headers.get("content-type", content_type)
    await session.send(
        "Fetch.fulfillRequest",
        {
            "requestId": rid,
            "responseCode": status,
            "responseHeaders": [{"name": k, "value": str(v)} for k, v in headers.items()],
            "body": base64.b64encode(body).decode("ascii"),
        },
        session_id=sid,
    )
    _emit(args, "rewritten", request)


def _call(hook, args, name, *hook_args):
    fn = hook.get(name)
    if not callable(fn):
        return None
    try:
        return fn(*hook_args)
    except Exception as e:
        # A broken hook must not wedge the page's traffic: let the request
        # through and say what happened.
        _emit(args, f"hook-error: {e!r}", {"url": hook_args[0].get("url"), "method": hook_args[0].get("method")})
        return None


def _encode_body(body):
    """(bytes, content-type) for a hook-returned body; dicts become JSON."""
    if body is None:
        return b"", "application/json"
    if isinstance(body, bytes):
        return body, "application/octet-stream"
    if isinstance(body, str):
        return body.encode("utf-8"), "text/plain; charset=utf-8"
    return json.dumps(body, ensure_ascii=False).encode("utf-8"), "application/json"


def _emit(args, action, request):
    if args.json:
        emit_ndjson({"action": action, "method": request.get("method"), "url": request.get("url")})
        return
    line = Text()
    line.append(datetime.now().strftime("%H:%M:%S "), style="dim")
    style = "yellow" if action == "mocked" else "cyan" if action == "rewritten" else "red"
    line.append(f"{action:9}", style=style)
    line.append(f" {(request.get('method') or '?'):6}", style="dim")
    line.append(" " + (request.get("url") or ""))
    console.print(line, no_wrap=True, overflow="ellipsis", crop=True)
