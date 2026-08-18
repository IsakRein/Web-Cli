# web-cli

A minimalist, agent-optimized CLI that drives the local debug Chrome (launching
it if needed) over the Chrome DevTools Protocol (CDP) on `127.0.0.1:9222`. It exists to give an
agent (or a shell script) four things the AppleScript-based `chrome-cli` could
not: run JS in a page, read cookies including HttpOnly, read local/session
storage, and — the marquee feature — **listen to a tab's network traffic live**
to capture an authenticated request (e.g. log in, click "download statement",
capture that request for saving or replay).

Output is Rich colored tables by default (like `tt` and the other CLIs). Pass
`--json` for machine-readable output — JSON for one-shot commands, NDJSON for
`listen`. Errors print as `error: ...` on stderr with a nonzero exit. No
tracebacks ever reach the caller, so it composes cleanly in pipelines.

## Install

Installed on every machine by `~/.dotfiles/bootstrap.sh` as a git-pinned uv tool
(`uv_git_tool web-cli web-cli Projects.Web-Cli`). For local development:

```bash
uv tool install --force .   # puts `web-cli` on PATH
# or run without installing:
uv run web-cli tabs
```

## Requirements

The debug Chrome — see [The debug Chrome dependency](#the-debug-chrome-dependency)
below. It does not need to be running: if `:9222` is unreachable, every command
launches `/Applications/Chrome.app` itself (via `open -g`, so it never steals
focus) and waits up to 20s for CDP to come up. Exit `3` now means the launch
failed: the app is missing (re-run bootstrap) or CDP never appeared.

## Commands

Tab selectors `-t/--target-id <id>` and `--url <substr>` go **after** the
subcommand. With neither, commands act on the active (first) tab.

Add `--json` to any command for machine-readable output.

```bash
web-cli tabs                          # table of page targets (list is an alias)
web-cli tabs --json                   # [{id,title,url,active}]

web-cli open https://example.com      # new BACKGROUND tab; prints {id,url} to target next
web-cli open --foreground https://example.com   # focus the new tab (interrupts the user)
web-cli nav https://example.com --url old       # navigate the matching tab, wait for load
web-cli close --url example           # close the matching tab

web-cli eval 'document.title'         # run JS, print the value
web-cli eval -t 8C1B 'location.href'  # target by id (exact or unique prefix)
web-cli eval --url github 'document.querySelector("h1").innerText'

web-cli screenshot page.png           # capture the active tab (shot is an alias)
web-cli screenshot --url localhost --full-page page.png   # whole page; also rasters occluded tabs
web-cli screenshot --activate page.png                    # focus first: escape hatch for blank captures

web-cli record demo.mp4 --duration 10 # record a tab (needs ffmpeg on PATH to assemble;
                                      # without it the frames + assemble command are kept)
web-cli record --activate demo.mp4    # occluded tabs are throttled and may send no frames
web-cli record --activate --duration 60 --run 'demoScript()' demo.mp4
                                      # drive the page while recording: the JS runs over the
                                      # recording's own CDP session (promises awaited) and the
                                      # recording stops ~1.5s after it settles. Use this instead
                                      # of a concurrent `web-cli eval`, which stalls the frames.
                                      # Recordings survive page navigations (screencast re-arms).

web-cli cookies                       # cookies for the active tab (incl. HttpOnly)
web-cli cookies --url github          # cookies for the tab matching "github"
web-cli cookies --all-tabs            # browser-wide cookie jar

web-cli storage                       # local + session storage
web-cli storage --type local          # just localStorage

web-cli listen --duration 5           # colored live network log for 5s
web-cli listen --json --duration 5    # NDJSON, one line per request
web-cli listen                        # stream until Ctrl-C
```

### `listen` — network capture

Attaches to a tab, enables the Network domain, and prints one JSON line per
completed request. Each record carries the request URL/method, request headers
(incl. `Cookie`/`Authorization`, merged from CDP's extra-info events), POST body,
response status/headers, MIME type, and a best-effort response body.

```bash
# Bank-statement capture: start listening, then log in and click download.
web-cli listen --url mybank --save-dir ./cap --curl
#   -> the statement request captured with auth headers; body written to ./cap/;
#      each record includes a replayable `curl ...` you can run outside Chrome.

# Flags:
#   --duration N     stop after N seconds (default: until Ctrl-C)
#   --match SUBSTR   only requests whose URL contains SUBSTR
#   --method GET     only this HTTP method
#   --type XHR       only this CDP resourceType (Document, Script, XHR, Fetch, ...)
#   --save-dir DIR   write response bodies to DIR (respects the filters above)
#   --reload         reload the tab first (capture a full page load)
#   --curl           add a replayable curl string to each record
```

Note: `listen` only sees requests that happen **after** it attaches — start it
before triggering the action you want to capture. True browser downloads that
stream straight to disk expose no retrievable body via CDP; the record still
captures the full request + headers, and `--curl` reproduces the download.

## Exit codes

`0` ok, `1` command error (e.g. a JS exception), `3` Chrome unreachable (and
auto-launching it failed), `4` no matching tab.

## The debug Chrome dependency

`web-cli` talks to the real Chrome (real profile, real logins) over CDP. That is
provided by a small launcher app, NOT by anything in this repo:

- **App:** `/Applications/Chrome.app` (displayed as just "Chrome"; the real
  browser is `Google Chrome.app`, kept hidden). Bundle id
  `com.isak.chrome-debugger`.
- **What it does:** execs Google Chrome with `--remote-debugging-port=9222`
  against a relocated `Chrome-Debug` profile, exposing CDP on `127.0.0.1:9222`.
  It execs (does not wrap) Chrome, so Chrome's code signature stays intact and
  Keychain is not broken.
- **Why a relocated profile:** since Chrome 136 the browser silently ignores
  `--remote-debugging-port` on the default user-data-dir (verified on Chrome
  150/152), so a separate `Chrome-Debug` profile is used. Chrome Sync keeps it
  current after the one-time seed from the real profile.
- **Default browser:** the app declares itself an http/https handler and is set
  as the default browser, so every clicked link and cold start lands on the
  debug profile at :9222.

Built and owned end to end by the `Chrome.app` section (2.5) of
`~/.dotfiles/bootstrap.sh` (idempotent — re-running repairs the app). To revert:
`chflags nohidden "/Applications/Google Chrome.app"`, then re-run bootstrap.
