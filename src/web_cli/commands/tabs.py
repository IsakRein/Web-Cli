"""List page targets (tabs). Rich table by default, JSON with --json.

First entry is the default selection (marked "active"). Chrome orders the list
by target MRU, not window focus — a tab just created by `open` sits first even
though it opened in the background and the user's focused tab didn't change.
Each row carries its Chrome profile; --profile lists one profile's tabs only.
"""

from rich import box
from rich.table import Table
from rich.text import Text

from ..discovery import list_targets, resolve_profile, tag_profiles
from ..output import console, emit_json


async def run(args, conn):
    targets = await list_targets(conn)
    info = await tag_profiles(conn, targets) if targets else {}
    if args.profile:
        want = resolve_profile(info, args.profile)
        targets = [t for t in targets if t["profile"] == want]
    rows = [
        {
            "id": t["id"], "title": t["title"], "url": t["url"], "active": i == 0,
            "profile": t["profile"], "profile_name": info.get(t["profile"], {}).get("name"),
        }
        for i, t in enumerate(targets)
    ]

    if args.json:
        emit_json(rows)
        return

    table = Table(box=box.SIMPLE_HEAD, header_style="bold", expand=False)
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("", no_wrap=True)  # active marker
    table.add_column("Profile", no_wrap=True)
    table.add_column("Title")
    table.add_column("URL", style="dim")
    for r in rows:
        marker = Text("●", style="bold green") if r["active"] else Text(" ")
        title = Text(r["title"]) if r["title"] else Text("(untitled)", style="dim")
        table.add_row(r["id"][:8], marker, r["profile_name"] or r["profile"] or "?", title, r["url"])
    console.print(table)
