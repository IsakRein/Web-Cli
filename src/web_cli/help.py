"""Rich help screens matching the house style of `pm` / `tt`.

Rendered off the argparse structures so command/option lists never drift from
what the parser actually accepts.
"""

import argparse

from rich import box
from rich.table import Table

from . import __version__
from .output import console, err_console

PROG = "web-cli"
SUBTITLE = "drive the running Chrome over CDP (127.0.0.1:9222)"


class RichParser(argparse.ArgumentParser):
    """ArgumentParser whose usage errors print in the house `error:` style."""

    def error(self, message):
        prog = self.prog
        err_console.print(f"[bold red]error:[/bold red] {message}")
        err_console.print(f"[dim]run `{prog} -h` for usage[/dim]")
        self.exit(2)


def _subparsers(parser):
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a
    return None


def _table():
    t = Table(box=box.SIMPLE, show_header=False, expand=False, pad_edge=False)
    t.add_column("", style="cyan", no_wrap=True)
    t.add_column("", style="dim")
    return t


class RichHelp(argparse.Action):
    """Drop-in -h/--help that renders Rich instead of argparse's plain text."""

    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        if _subparsers(parser):
            print_main_help(parser)
        else:
            print_command_help(parser)
        parser.exit()


def print_main_help(parser):
    console.print(f"[bold]{PROG}[/] [dim]— {SUBTITLE} (v{__version__})[/]")
    console.print()
    console.print(f"[bold]Usage:[/] {PROG} [cyan]<command>[/] [dim]\\[args/options][/]")
    console.print()
    console.print("[bold]Commands[/]")
    table = _table()
    for ca in _subparsers(parser)._choices_actions:  # one row per command (metavar folds in aliases)
        table.add_row(ca.metavar or ca.dest, ca.help or "")
    console.print(table)
    console.print()
    console.print(f"Run [cyan]{PROG} <command> -h[/] for command options.")


def print_command_help(parser):
    usage = parser.format_usage().replace("usage: ", "").rstrip()
    console.print(f"[bold]Usage:[/] {usage}")
    if parser.description:
        console.print(f"[dim]{parser.description}[/dim]")

    positionals = [a for a in parser._actions if not a.option_strings]
    optionals = [a for a in parser._actions if a.option_strings]

    if positionals:
        console.print()
        console.print("[bold]Arguments[/]")
        t = _table()
        for a in positionals:
            t.add_row(a.metavar or a.dest, a.help or "")
        console.print(t)

    console.print()
    console.print("[bold]Options[/]")
    t = _table()
    for a in optionals:
        takes_value = a.nargs != 0
        meta = f" {a.metavar or a.dest.upper()}" if takes_value else ""
        t.add_row(", ".join(a.option_strings) + meta, a.help or "")
    console.print(t)
