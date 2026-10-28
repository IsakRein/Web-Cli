"""Stream a tab's network requests as NDJSON while you interact with it.

Attaches to the tab, enables the Network domain, and prints one JSON line per
completed request until Ctrl-C or --duration. The point: start listening, log in,
click "download statement", and capture that authenticated request (URL + headers
incl. Cookie/Authorization + body) so it can be saved or replayed with --curl.
"""

import asyncio
import base64
import contextlib
import os
import re
from datetime import datetime
from urllib.parse import urlsplit

from rich.text import Text

from ..cdp import CDPError, open_target
from ..discovery import pick_target
from ..output import console, emit_ndjson

INLINE_CAP = 64 * 1024  # inline text bodies up to this size when not saving to disk


async def run(args, conn):
    target = await pick_target(conn, args)
    save_dir = os.path.expanduser(args.save_dir) if args.save_dir else None
    if save_dir:
        os.makedirs(save_dir, exist_ok=True)

    if not args.json:
        stop = f"{args.duration}s" if args.duration else "Ctrl-C to stop"
        console.print(
            f"[dim]listening on[/dim] {target['url']}  [dim]({stop})[/dim]",
            no_wrap=True, overflow="ellipsis", crop=True,
        )

    inflight = {}
    async with open_target(conn, target) as (session, sid):
        await session.send("Network.enable", session_id=sid)
        if args.reload:
            await session.send("Page.enable", session_id=sid)
            await session.send("Page.reload", session_id=sid)

        drain = _drain(session, sid, args, save_dir, inflight)
        try:
            if args.duration:
                async with asyncio.timeout(args.duration):
                    await drain
            else:
                await drain
        except (TimeoutError, asyncio.CancelledError, KeyboardInterrupt):
            pass  # clean stop; completed requests were already emitted live


async def _drain(session, sid, args, save_dir, inflight):
    emitted = 0
    async for msg in session.events():
        method = msg.get("method")
        p = msg.get("params", {})
        rid = p.get("requestId")
        if not rid:
            continue

        if method == "Network.requestWillBeSent":
            req = p.get("request", {})
            rec = inflight.setdefault(rid, _new_rec(rid))
            rec["url"] = req.get("url", rec.get("url"))
            rec["method"] = req.get("method", rec.get("method"))
            rec["resource_type"] = p.get("type", rec.get("resource_type"))
            rec["request_headers"] = _merge(rec.get("request_headers"), req.get("headers"))
            if req.get("postData") is not None:
                rec["post_data"] = req["postData"]
            elif req.get("hasPostData"):
                rec["_needs_post"] = True

        elif method == "Network.requestWillBeSentExtraInfo":
            rec = inflight.setdefault(rid, _new_rec(rid))
            rec["request_headers"] = _merge(rec.get("request_headers"), p.get("headers"))

        elif method == "Network.responseReceived":
            resp = p.get("response", {})
            rec = inflight.setdefault(rid, _new_rec(rid))
            rec["status"] = resp.get("status")
            rec["mime_type"] = resp.get("mimeType")
            rec["remote_ip"] = resp.get("remoteIPAddress")
            rec["response_headers"] = _merge(rec.get("response_headers"), resp.get("headers"))

        elif method == "Network.responseReceivedExtraInfo":
            rec = inflight.setdefault(rid, _new_rec(rid))
            rec["response_headers"] = _merge(rec.get("response_headers"), p.get("headers"))
            if rec.get("status") is None:
                rec["status"] = p.get("statusCode")

        elif method == "Network.loadingFinished":
            rec = inflight.pop(rid, None)
            if rec is None or not _matches(rec, args):
                continue
            if rec.pop("_needs_post", False):
                rec["post_data"] = await _post_data(session, sid, rid)
            await _attach_body(session, sid, rid, rec, save_dir)
            _emit(rec, args)
            emitted += 1
            if args.first and emitted >= args.first:
                return

        elif method == "Network.loadingFailed":
            rec = inflight.pop(rid, None)
            if rec is None or not _matches(rec, args):
                continue
            rec["failed"] = p.get("errorText", "failed")
            _emit(rec, args)
            emitted += 1
            if args.first and emitted >= args.first:
                return


def _new_rec(rid):
    return {
        "requestId": rid,
        "url": None,
        "method": None,
        "resource_type": None,
        "request_headers": {},
        "post_data": None,
        "status": None,
        "response_headers": {},
        "mime_type": None,
        "remote_ip": None,
    }


async def _post_data(session, sid, rid):
    try:
        res = await session.send("Network.getRequestPostData", {"requestId": rid}, session_id=sid)
        return res.get("postData")
    except CDPError:
        return None


async def _attach_body(session, sid, rid, rec, save_dir):
    try:
        res = await session.send("Network.getResponseBody", {"requestId": rid}, session_id=sid)
    except CDPError as e:
        # Streamed downloads and some responses expose no retrievable body.
        rec["body"] = None
        rec["body_error"] = e.message
        return

    body = res.get("body", "")
    b64 = res.get("base64Encoded", False)
    data = base64.b64decode(body) if b64 else body.encode("utf-8")
    rec["body_error"] = None

    if save_dir:
        path = _save(save_dir, rec, data)
        rec["body_file"] = path
        rec["body"] = None
    elif not b64 and len(data) <= INLINE_CAP:
        rec["body"] = body
    else:
        rec["body"] = None
        rec["body_bytes"] = len(data)


def _matches(rec, args):
    if args.method and (rec.get("method") or "").upper() != args.method.upper():
        return False
    if args.type and (rec.get("resource_type") or "").lower() != args.type.lower():
        return False
    if args.match and args.match.lower() not in (rec.get("url") or "").lower():
        return False
    return True


def _emit(rec, args):
    if args.curl:
        rec["curl"] = _curl(rec)
    if args.json:
        emit_ndjson(rec)
    else:
        _print_human(rec, args)


def _print_human(rec, args):
    line = Text()
    line.append(datetime.now().strftime("%H:%M:%S "), style="dim")
    line.append(f"{(rec.get('method') or '?'):6}", style=_method_style(rec.get("method")))
    line.append(_status_text(rec))
    line.append(f" {(rec.get('resource_type') or ''):9}", style="dim")
    line.append(" " + (rec.get("url") or ""))
    if rec.get("body_file"):
        line.append("  -> saved", style="green")
    console.print(line, no_wrap=True, overflow="ellipsis", crop=True)
    if args.curl and rec.get("curl"):
        console.print("  " + rec["curl"], style="dim", no_wrap=True, overflow="ellipsis", crop=True)


def _method_style(method):
    return {"GET": "white", "POST": "yellow", "PUT": "yellow", "DELETE": "red"}.get(
        (method or "").upper(), "cyan"
    )


def _status_text(rec):
    if rec.get("failed"):
        return Text(" FAIL", style="bold red")
    s = rec.get("status")
    if s is None:
        return Text("   - ", style="dim")
    if s < 300:
        style = "green"
    elif s < 400:
        style = "cyan"
    else:
        style = "red"
    return Text(f" {s} ", style=style)


def _merge(base, extra):
    """Case-insensitive header merge; later source wins the value, first the casing."""
    out = {}
    seen = {}
    for src in (base or {}), (extra or {}):
        for k, v in src.items():
            lk = k.lower()
            if lk in seen:
                out[seen[lk]] = v
            else:
                seen[lk] = k
                out[k] = v
    return out


def _save(save_dir, rec, data):
    name = _attachment_name(rec) or _url_name(rec.get("url")) or "body"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120]
    path = os.path.join(save_dir, f"{rec['requestId']}__{safe}")
    with open(path, "wb") as f:
        f.write(data)
    return path


def _attachment_name(rec):
    for k, v in (rec.get("response_headers") or {}).items():
        if k.lower() == "content-disposition":
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', v)
            if m:
                return m.group(1)
    return None


def _url_name(url):
    if not url:
        return None
    seg = urlsplit(url).path.rsplit("/", 1)[-1]
    return seg or None


def _curl(rec):
    import shlex

    parts = ["curl", "-sL", shlex.quote(rec.get("url") or "")]
    method = (rec.get("method") or "GET").upper()
    if method != "GET":
        parts += ["-X", method]
    for k, v in (rec.get("request_headers") or {}).items():
        if k.startswith(":") or k.lower() in ("content-length", "host"):
            continue
        parts += ["-H", shlex.quote(f"{k}: {v}")]
    if rec.get("post_data"):
        parts += ["--data-raw", shlex.quote(rec["post_data"])]
    out = _attachment_name(rec) or _url_name(rec.get("url"))
    if out:
        parts += ["-o", shlex.quote(re.sub(r"[^A-Za-z0-9._-]", "_", out)[:120])]
    return " ".join(parts)
