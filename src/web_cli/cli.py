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
from .commands import cookies, eval_js, listen, navigate, storage, tabs
from .discovery import find_browser
from .help import RichHelp, RichParser, print_main_help
from .output import Die, report

_HELP = ["-h", "--help"]


def _parser(add_help=True, **kwargs):
    """An argparse parser with argparse's plain -h swapped for the Rich one."""
    p = RichParser(add_help=False, **kwargs)
    if add_help:
        p.add_argument(*_HELP, action=RichHelp, help="show this help")
    return p


def _build_parser():
    parser = _parser(prog="web-cli")
    parser.add_argument("--version", action="version", version=f"web-cli {__version__}")

    # Shared output flag, inherited by every command.
    fmt = _parser(add_help=False)
    fmt.add_argument("--json", action="store_true", help="emit JSON (uncolored, machine-readable)")

    # Shared tab selectors, inherited by commands that act on a tab.
    sel = _parser(add_help=False)
    sel.add_argument("-t", "--target-id", help="target/tab id (exact or unique prefix)")
    sel.add_argument("--url", help="pick the first tab whose URL contains this substring")

    sub = parser.add_subparsers(dest="command")

    def command(name, handler, help, parents, **kw):
        p = sub.add_parser(name, parents=[*parents], help=help, add_help=False, description=help, **kw)
        p.add_argument(*_HELP, action=RichHelp, help="show this help")
        p.set_defaults(handler=handler)
        return p

    command("tabs", tabs.run, "list page targets (tabs)", [fmt], aliases=["list"])

    p = command("open", navigate.open_run, "open a new tab at a URL", [fmt])
    p.add_argument("address", metavar="url", help="URL to open in a new tab")

    p = command("nav", navigate.nav_run, "navigate a tab to a URL and wait for load", [fmt, sel])
    p.add_argument("address", metavar="url", help="URL to navigate the tab to")
    p.add_argument("--no-wait", action="store_true", help="don't wait for the load event")
    p.add_argument("--timeout", type=float, default=30, help="max seconds to wait for load (default: 30)")

    p = command("close", navigate.close_run, "close a tab", [fmt, sel])

    p = command("eval", eval_js.run, "run JS in a tab, print the result", [fmt, sel])
    p.add_argument("js", help="JavaScript expression; its value is returned")
    p.add_argument("--timeout", type=float, default=30, help="max seconds to await the result (default: 30)")

    p = command("cookies", cookies.run, "read cookies incl. HttpOnly", [fmt, sel])
    p.add_argument("--all-tabs", action="store_true", help="browser-wide cookie jar")

    p = command("storage", storage.run, "read local/session storage", [fmt, sel])
    p.add_argument("--type", choices=["local", "session", "all"], default="all",
                   help="which store to read (default: all)")

    p = command("listen", listen.run, "stream a tab's network requests live", [fmt, sel])
    p.add_argument("--duration", type=float, help="stop after N seconds (default: until Ctrl-C)")
    p.add_argument("--first", type=int, help="stop after N matching requests (one-shot capture)")
    p.add_argument("--match", help="only requests whose URL contains this substring")
    p.add_argument("--method", help="only requests with this HTTP method")
    p.add_argument("--type", dest="type", help="only this resourceType (xhr, fetch, document, ...)")
    p.add_argument("--save-dir", help="write response bodies to this directory")
    p.add_argument("--reload", action="store_true", help="reload the tab first")
    p.add_argument("--curl", action="store_true", help="add a replayable curl to each record")

    return parser


async def _dispatch(args):
    conn = find_browser()
    await args.handler(args, conn)


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        print_main_help(parser)
        sys.exit(0)
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
