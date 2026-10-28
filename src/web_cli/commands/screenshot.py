"""Capture a tab as a PNG.

Made for automation screenshotting its own background tab without stealing
focus: `--full-page` passes captureBeyondViewport, which also forces Chrome to
raster an occluded tab. A plain viewport capture of a background tab can come
back blank or error on some GPU/occlusion setups; `--activate` focuses the tab
first as the escape hatch — the one command in this CLI allowed to grab focus,
and only on request.
"""

import asyncio
import base64
from datetime import datetime
from pathlib import Path

from ..cdp import CDPError, open_target
from ..discovery import pick_target
from ..output import console, die, emit_json


def _default_path(target):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"web-cli-{target['id'][:8].lower()}-{stamp}.png"


async def run(args, conn):
    target = await pick_target(conn, args)
    path = Path(args.output or _default_path(target))

    async with open_target(conn, target) as (session, sid):
        if args.activate:
            # The page's own session accepts this (it is how Puppeteer's
            # bringToFront works), so both connection modes are covered.
            await session.send(
                "Target.activateTarget", {"targetId": target["id"]}, session_id=sid
            )
            # A capture fired in the same beat as activation races the first
            # raster and dies with "Internal error"; give the compositor a
            # moment to paint the newly visible tab.
            await asyncio.sleep(0.5)
        try:
            res = await session.send(
                "Page.captureScreenshot",
                {"format": "png", "captureBeyondViewport": bool(args.full_page)},
                session_id=sid,
                timeout=args.timeout,
            )
        except CDPError as e:
            die(
                f"Screenshot failed: {e.message}. An occluded tab usually "
                "captures with --full-page; --activate is the escape hatch.",
                code=1,
            )

    data = base64.b64decode(res["data"])
    path.write_bytes(data)

    row = {
        "path": str(path),
        "bytes": len(data),
        "id": target["id"],
        "url": target.get("url", ""),
    }
    if args.json:
        emit_json(row)
    else:
        console.print(f"{row['bytes']} bytes -> {row['path']}")
