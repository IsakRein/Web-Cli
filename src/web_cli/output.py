"""Output helpers. Rich (colored tables) is the default; --json is the machine mode.

Data goes to stdout (Rich or JSON); errors go to stderr, styled like the other
CLIs (`error: ...` in bold red). No Python tracebacks ever reach the caller.

Exit codes:
  0  ok
  1  command-level error (JS exception, hard body-fetch failure)
  3  Chrome debug endpoint unreachable
  4  no matching target
"""

import json
import sys

from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def emit_json(obj):
    """Print one JSON document (the command's result) to stdout, uncolored."""
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def emit_ndjson(obj):
    """Print one JSON object as a line, flushed so streamed consumers see it live."""
    print(json.dumps(obj, ensure_ascii=False, default=str), flush=True)


class Die(Exception):
    """Raised to abort with a styled error on stderr and a specific exit code.

    cli.main() catches this so the message is emitted exactly once, from the top
    level, no matter how deep the call stack is when it happens.
    """

    def __init__(self, msg, code=1):
        super().__init__(msg)
        self.msg = msg
        self.code = code


def die(msg, code=1):
    raise Die(msg, code)


def report(err):
    """Write an error to stderr, matching the ecosystem's `error: ...` style."""
    err_console.print(f"[bold red]error:[/bold red] {err}")
