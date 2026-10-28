"""Read cookies for a tab, including HttpOnly (which document.cookie cannot see).

Uses CDP Network.getCookies scoped to the tab. Pass --url to restrict to a URL;
--all-tabs switches to the whole cookie jar of the tab's profile
(Network.getAllCookies: Storage.getCookies only ever reads the last-used
profile's jar). Rich table by default, JSON with --json.
"""

from rich import box
from rich.table import Table
from rich.text import Text

from ..cdp import open_target
from ..discovery import pick_target
from ..output import console, emit_json


async def run(args, conn):
    target = await pick_target(conn, args)
    async with open_target(conn, target) as (session, sid):
        await session.send("Network.enable", session_id=sid)
        if getattr(args, "all_tabs", False):
            res = await session.send("Network.getAllCookies", session_id=sid)
        elif args.url:
            res = await session.send(
                "Network.getCookies", {"urls": [_full_url(args.url, target)]},
                session_id=sid,
            )
        else:
            res = await session.send("Network.getCookies", session_id=sid)
    cookies = res.get("cookies", [])

    if args.json:
        emit_json(cookies)
        return

    table = Table(box=box.SIMPLE_HEAD, header_style="bold", expand=False)
    table.add_column("Name", no_wrap=True)
    table.add_column("Value", overflow="ellipsis", max_width=48)
    table.add_column("Domain", style="dim", no_wrap=True)
    table.add_column("Path", style="dim", no_wrap=True)
    table.add_column("Flags", no_wrap=True)
    for c in cookies:
        flags = Text()
        if c.get("httpOnly"):
            flags.append("httpOnly ", style="yellow")
        if c.get("secure"):
            flags.append("secure ", style="cyan")
        if c.get("session"):
            flags.append("session", style="dim")
        table.add_row(
            c.get("name", ""),
            c.get("value", ""),
            c.get("domain", ""),
            c.get("path", ""),
            flags,
        )
    console.print(table)
    console.print(f"[dim]{len(cookies)} cookies[/dim]")


def _full_url(url, target):
    """--url may be a bare substring; if it isn't a URL, fall back to the tab's URL."""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return target["url"]
