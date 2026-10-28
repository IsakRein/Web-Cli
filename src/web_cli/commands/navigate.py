"""Open, navigate, and close tabs.

`open`  — new tab at a URL (prints its id, so scripts can target it next).
          Background by default so automation never steals the user's focus;
          --foreground opts back into focusing it. --profile picks the Chrome
          profile (see discovery.py for how).
`nav`   — point an existing tab at a URL and wait for load.
`close` — close a tab.
"""

import asyncio

from ..cdp import open_target
from ..discovery import close_target, list_targets, new_target, pick_target, select_target
from ..output import console, emit_json


def _report(target, args):
    row = {"id": target["id"], "url": target.get("url", "")}
    if target.get("profile"):
        row["profile"] = target["profile"]
    if args.json:
        emit_json(row)
    else:
        console.print(f"{target['id'][:8]}  {target.get('url', '')}")


async def open_run(args, conn):
    target = await new_target(conn, args.address, not args.foreground, args.profile)
    _report(target, args)


async def nav_run(args, conn):
    target = await pick_target(conn, args)
    async with open_target(conn, target) as (session, sid):
        await session.send("Page.enable", session_id=sid)
        await session.send("Page.navigate", {"url": args.address}, session_id=sid)
        if not args.no_wait:
            await _wait_load(session, args.timeout)
    # Re-read so the printed URL reflects where we actually landed.
    _report(select_target(await list_targets(conn), target["id"], None), args)


async def close_run(args, conn):
    target = await pick_target(conn, args)
    await close_target(conn, target["id"])
    if args.json:
        emit_json({"closed": target["id"]})
    else:
        console.print(f"closed {target['id'][:8]}")


async def _wait_load(session, timeout):
    """Block until the tab fires its load event, or the timeout elapses."""
    try:
        async with asyncio.timeout(timeout):
            async for msg in session.events():
                if msg.get("method") == "Page.loadEventFired":
                    return
    except TimeoutError:
        pass  # navigation committed; the page just took longer than we waited
