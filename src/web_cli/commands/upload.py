"""Attach local files to a file input, as if the user had picked them.

A page script cannot do this — `input.files` is read-only to JS — but CDP can:
`DOM.setFileInputFiles` sets the input's files and fires its change event, so
the page's own upload flow runs exactly as after a real file-picker. That is
the whole point: upload through a site's UI with the logged-in session (a
video to YouTube Studio, a receipt to a portal) where no usable API exists.

The selector is matched deep — through open shadow roots too — because the
web-component apps that most need this (Studio is Polymer) keep their inputs
inside them, out of reach of a plain `document.querySelector`. The first match
wins; `input[type=file]` is the default, which is right whenever a page has
one picker open.
"""

import json
from pathlib import Path

from ..cdp import open_target
from ..discovery import pick_target
from ..output import console, die, emit_json

# Breadth-first through the document and every open shadow root.
_DEEP_QUERY = """
(selector => {
  const roots = [document];
  while (roots.length) {
    const root = roots.shift();
    const hit = root.querySelector(selector);
    if (hit) return hit;
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) roots.push(el.shadowRoot);
  }
  return null;
})(%s)
"""


async def run(args, conn):
    files = [Path(f).expanduser().resolve() for f in args.files]
    for f in files:
        if not f.is_file():
            die(f"No such file: {f}", code=1)

    target = await pick_target(conn, args)
    async with open_target(conn, target) as (session, sid):
        res = await session.send(
            "Runtime.evaluate",
            {"expression": _DEEP_QUERY % json.dumps(args.selector)},
            session_id=sid,
        )
        obj = res.get("result", {})
        if obj.get("subtype") == "null" or "objectId" not in obj:
            die(f"No element matches {args.selector!r} in the page (shadow roots included).",
                code=1)
        if "input" not in (obj.get("description") or "").lower():
            die(f"{args.selector!r} matched {obj.get('description')}, not a file input.", code=1)
        await session.send(
            "DOM.setFileInputFiles",
            {"files": [str(f) for f in files], "objectId": obj["objectId"]},
            session_id=sid,
        )

    row = {"files": [str(f) for f in files], "selector": args.selector,
           "id": target["id"], "url": target.get("url", "")}
    if args.json:
        emit_json(row)
    else:
        console.print(f"attached {len(files)} file(s) to {args.selector}")
