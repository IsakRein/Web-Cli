"""Read a tab's localStorage and/or sessionStorage. Rich by default, JSON with --json."""

from urllib.parse import urlsplit

from rich import box
from rich.table import Table

from ..cdp import open_target
from ..discovery import list_targets, select_target
from ..output import console, die, emit_json


async def run(args, conn):
    target = select_target(await list_targets(conn), args.target_id, args.url)
    origin = _origin(target["url"])
    if not origin:
        die(f"Tab has no web origin to read storage from (url: {target['url']}).", 1)

    want_local = args.type in ("local", "all")
    want_session = args.type in ("session", "all")
    out = {"origin": origin}

    async with open_target(conn, target) as (session, sid):
        await session.send("DOMStorage.enable", session_id=sid)
        if want_local:
            out["local"] = await _items(session, sid, origin, True)
        if want_session:
            out["session"] = await _items(session, sid, origin, False)

    if args.json:
        emit_json(out)
        return

    console.print(f"[bold]{origin}[/bold]")
    for bucket in ("local", "session"):
        if bucket in out:
            _print_bucket(bucket, out[bucket])


def _print_bucket(name, items):
    table = Table(
        title=f"{name}Storage",
        title_justify="left",
        title_style="bold",
        box=box.SIMPLE,
        header_style="dim",
        expand=False,
        pad_edge=False,
    )
    table.add_column("Key", no_wrap=True)
    table.add_column("Value", overflow="ellipsis", max_width=64)
    if not items:
        table.add_row("[dim](empty)[/dim]", "")
    for k, v in items.items():
        table.add_row(k, v)
    console.print(table)


async def _items(session, sid, origin, is_local):
    res = await session.send(
        "DOMStorage.getDOMStorageItems",
        {"storageId": {"securityOrigin": origin, "isLocalStorage": is_local}},
        session_id=sid,
    )
    # entries come back as [[key, value], ...]
    return {k: v for k, v in res.get("entries", [])}


def _origin(url):
    parts = urlsplit(url)
    if parts.scheme in ("http", "https") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return None
