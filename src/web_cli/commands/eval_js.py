"""Run JavaScript in a tab and print the returned value as JSON."""

from ..cdp import open_target
from ..discovery import list_targets, select_target
from ..output import console, die, emit_json


async def run(args, conn):
    target = select_target(await list_targets(conn), args.target_id, args.url)
    async with open_target(conn, target) as (session, sid):
        res = await session.send(
            "Runtime.evaluate",
            {
                "expression": args.js,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
            },
            session_id=sid,
        )

    if "exceptionDetails" in res:
        exc = res["exceptionDetails"]
        detail = exc.get("exception", {}).get("description") or exc.get("text", "")
        die(f"JS error: {detail}", code=1)

    # returnByValue gives a plain JSON value; undefined has no "value" key.
    value = res.get("result", {}).get("value")

    if args.json:
        emit_json(value)
    elif isinstance(value, (dict, list)):
        console.print_json(data=value)  # syntax-highlighted
    else:
        console.print(value)  # scalar: string / number / bool / None
