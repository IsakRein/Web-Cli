"""List page targets (tabs). Rich table by default, JSON with --json.

First entry is the default/active selection.
"""

from rich import box
from rich.table import Table
from rich.text import Text

from ..discovery import list_targets
from ..output import console, emit_json


async def run(args, conn):
    targets = await list_targets(conn)
    rows = [
        {"id": t["id"], "title": t["title"], "url": t["url"], "active": i == 0}
        for i, t in enumerate(targets)
    ]

    if args.json:
        emit_json(rows)
        return

    table = Table(box=box.SIMPLE_HEAD, header_style="bold", expand=False)
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("", no_wrap=True)  # active marker
    table.add_column("Title")
    table.add_column("URL", style="dim")
    for r in rows:
        marker = Text("●", style="bold green") if r["active"] else Text(" ")
        title = Text(r["title"]) if r["title"] else Text("(untitled)", style="dim")
        table.add_row(r["id"][:8], marker, title, r["url"])
    console.print(table)
