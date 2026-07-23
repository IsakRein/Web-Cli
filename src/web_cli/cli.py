"""web-cli: drive the running debug Chrome over CDP. Agent-optimized, JSON output.

Data goes to stdout as JSON (or NDJSON for `listen`); errors go to stderr as
{"error": ...}. No Python tracebacks ever reach the caller.
"""

import argparse
import asyncio
import sys

import websockets

from . import __version__
from .cdp import CDPError
from .commands import cookies, eval_js, listen, storage, tabs
from .discovery import find_browser
from .output import Die, report


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="web-cli",
        description="Minimalist CLI driving the running Chrome over CDP (127.0.0.1:9222).",
    )
    parser.add_argument("--version", action="version", version=f"web-cli {__version__}")

    # Shared output flag, inherited by every command.
    fmt = argparse.ArgumentParser(add_help=False)
    fmt.add_argument("--json", action="store_true", help="emit JSON (uncolored, machine-readable)")

    # Shared tab selectors, inherited by commands that act on a tab.
    sel = argparse.ArgumentParser(add_help=False)
    sel.add_argument("-t", "--target-id", help="target/tab id (exact or unique prefix)")
    sel.add_argument("--url", help="pick the first tab whose URL contains this substring")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("tabs", aliases=["list"], parents=[fmt], help="list page targets (tabs)")
    p.set_defaults(handler=tabs.run)

    p = sub.add_parser("eval", parents=[fmt, sel], help="run JS in a tab, print the result")
    p.add_argument("js", help="JavaScript expression; its value is returned")
    p.set_defaults(handler=eval_js.run)

    p = sub.add_parser("cookies", parents=[fmt, sel], help="read cookies incl. HttpOnly")
    p.add_argument("--all-tabs", action="store_true", help="browser-wide cookie jar")
    p.set_defaults(handler=cookies.run)

    p = sub.add_parser("storage", parents=[fmt, sel], help="read local/session storage")
    p.add_argument("--type", choices=["local", "session", "all"], default="all")
    p.set_defaults(handler=storage.run)

    p = sub.add_parser(
        "listen", parents=[fmt, sel], help="stream a tab's network requests live"
    )
    p.add_argument("--duration", type=float, help="stop after N seconds (default: until Ctrl-C)")
    p.add_argument("--match", help="only requests whose URL contains this substring")
    p.add_argument("--method", help="only requests with this HTTP method")
    p.add_argument("--type", dest="type", help="only this resourceType (xhr, fetch, document, ...)")
    p.add_argument("--save-dir", help="write response bodies to this directory")
    p.add_argument("--reload", action="store_true", help="reload the tab first")
    p.add_argument("--curl", action="store_true", help="add a replayable curl to each record")
    p.set_defaults(handler=listen.run)

    return parser


async def _dispatch(args):
    conn = find_browser()
    await args.handler(args, conn)


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        asyncio.run(_dispatch(args))
    except Die as e:
        report(e.msg)
        sys.exit(e.code)
    except KeyboardInterrupt:
        sys.exit(0)
    except CDPError as e:
        report(e.message)
        sys.exit(1)
    except (OSError, websockets.exceptions.WebSocketException) as e:
        report(str(e))
        sys.exit(3)
