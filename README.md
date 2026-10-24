# web-cli

Drive your real Chrome from the command line over the Chrome DevTools Protocol
(CDP). Run JavaScript in a tab, take screenshots and recordings, read cookies
(HttpOnly included) and storage, mock API responses, and stream a tab's network
traffic live, all with your actual profile and logins.

It is built for coding agents and shell scripts. Every command has `--json`
output, errors are a single `error: ...` line on stderr with a meaningful exit
code, and no traceback ever reaches the caller. Tabs open in the background by
default, so an agent can work in your browser without stealing focus.

```console
$ web-cli open https://news.ycombinator.com
$ web-cli eval --url ycombinator 'document.querySelectorAll(".titleline").length'
30
$ web-cli listen --url mybank --match statement --curl --save-dir ./cap
# log in, click "download statement": the request is captured with its auth
# headers, the body is saved to ./cap/, and you get a replayable curl command
```

## Why

AppleScript-based tools like `chrome-cli` can list and open tabs, but they
cannot read HttpOnly cookies, see network traffic, or intercept requests.
Headless automation (Playwright, Puppeteer) can, but runs a fresh browser with
none of your sessions. web-cli attaches to the Chrome you already use, so
"capture the request my bank makes when I click download" is one command.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/IsakRein/Web-Cli
```

For development, from a checkout:

```bash
uv run web-cli tabs            # run without installing
uv tool install --force .      # or put this checkout's `web-cli` on PATH
```

## Setup: Chrome with a debugging port

web-cli connects to CDP at `127.0.0.1:9222`. Chrome only exposes it when
started with `--remote-debugging-port`, and since Chrome 136 it ignores that
flag on the default profile, so use a separate user-data-dir:

```bash
# macOS
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/Library/Application Support/Google/Chrome-Debug"

# Linux
google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.config/chrome-debug"
```

Sign in to Chrome Sync once in that profile and it fills in with your
bookmarks, extensions and saved logins. Check it is up with
`curl -s 127.0.0.1:9222/json/version`.

Anything listening on `127.0.0.1:9222` can control the browser, including every
logged-in session, so only run this on a machine you trust and never expose the
port beyond localhost.

If nothing answers on `:9222`, web-cli tries to launch `/Applications/Chrome.app`
in the background and waits up to 20 seconds for CDP. That path is the launcher
app from [my setup](#how-i-run-it); on other machines, start Chrome yourself as
above.

## Usage

```bash
web-cli <command> [options]
web-cli <command> -h          # options for one command
```

Commands act on the active (first) tab unless you pick one. Tab selectors go
**after** the subcommand:

- `-t/--target-id <id>`: exact id or a unique prefix (from `web-cli tabs`)
- `--url <substr>`: the tab whose URL contains the substring

Add `--json` to any command for machine-readable output: JSON for one-shot
commands, NDJSON (one object per line) for the streaming ones.

## Commands

| Command | What it does |
| --- | --- |
| `tabs` (`list`) | List open tabs |
| `open` | Open a new tab at a URL, in the background |
| `nav` | Navigate a tab to a URL and wait for the load |
| `close` | Close a tab |
| `eval` | Run JavaScript in a tab and print the result |
| `screenshot` (`shot`) | Capture a tab as a PNG |
| `record` | Record a tab to MP4 |
| `upload` | Attach local files to a file input, as if picked |
| `cookies` | Read cookies, HttpOnly included |
| `storage` | Read localStorage and sessionStorage |
| `mock` | Intercept a tab's network through a Python hook file |
| `listen` | Stream a tab's network requests live |

### Tabs and navigation

```bash
web-cli tabs                                   # table of tabs
web-cli tabs --json                            # [{id, title, url, active}]
web-cli open https://example.com               # background tab; prints {id, url}
web-cli open --foreground https://example.com  # focus the new tab
web-cli nav https://example.com --url old      # navigate the matching tab, wait for load
web-cli close --url example
```

### Running JavaScript

```bash
web-cli eval 'document.title'
web-cli eval -t 8C1B 'location.href'
web-cli eval --url github 'document.querySelector("h1").innerText'
```

Promises are awaited, and the value is returned by value, so objects and arrays
print as JSON.

### Screenshots and recordings

```bash
web-cli screenshot page.png                           # visible viewport
web-cli screenshot --url localhost --full-page page.png
web-cli screenshot --activate page.png                # focus the tab first if a capture comes back blank

web-cli record demo.mp4 --duration 10
web-cli record --activate --duration 60 --run 'demoScript()' demo.mp4
web-cli record --offscreen demo.mp4
web-cli record --offscreen --tick-js 'pump()' --run 'demo()' demo.mp4
```

`record` uses CDP's screencast and needs `ffmpeg` on PATH to assemble the MP4.
Without it, the frames and the ffmpeg command are kept so you can assemble
later. Recordings survive page navigations.

- `--run JS` drives the page during the recording over the recording's own CDP
  session. The recording stops about 1.5 seconds after the script settles. Use
  it instead of a concurrent `web-cli eval`, which stalls the frames.
- Chrome throttles tabs that are hidden or covered, and they may send no
  frames. Either `--activate` the tab, or use `--offscreen`, which captures
  forced-raster screenshots (around 7 fps) and needs no focus.
- Hidden tabs also throttle their own timers to about 1 Hz. `--tick-js JS` is
  evaluated before every captured frame, so a demo script can advance from
  wall-clock time instead of `setTimeout`.

### File uploads

```bash
web-cli upload video.mp4              # the page's input[type=file]
web-cli upload -s '#avatar' a.png     # pick the input by CSS selector, through shadow roots
```

The page's own upload flow runs, as if a user picked the file; the `change`
event fires.

### Cookies and storage

```bash
web-cli cookies                       # cookies for the active tab, HttpOnly included
web-cli cookies --url github
web-cli cookies --all-tabs            # the whole browser cookie jar
web-cli storage                       # localStorage + sessionStorage
web-cli storage --type local
```

### `listen`: network capture

Attaches to a tab and prints one record per completed request: URL, method,
request headers (including `Cookie` and `Authorization`), POST body, response
status and headers, MIME type, and the response body where Chrome still has
it.

```bash
web-cli listen --duration 5                        # colored live log for 5 seconds
web-cli listen --json                              # NDJSON until Ctrl-C
web-cli listen --url mybank --save-dir ./cap --curl
```

| Flag | Effect |
| --- | --- |
| `--duration N` | Stop after N seconds (default: until Ctrl-C) |
| `--match SUBSTR` | Only requests whose URL contains SUBSTR |
| `--method GET` | Only this HTTP method |
| `--type XHR` | Only this resource type (`Document`, `Script`, `XHR`, `Fetch`, ...) |
| `--save-dir DIR` | Write response bodies to DIR (respects the filters) |
| `--reload` | Reload the tab first, to capture a full page load |
| `--curl` | Add a replayable `curl` command to each record |

`listen` only sees requests made after it attaches, so start it before
triggering the action. Real browser downloads stream to disk and have no body
CDP can return, but the record still has the full request and headers, and
`--curl` reproduces the download outside Chrome.

### `mock`: network interception

Routes a tab's requests through a Python hook file, so you can demo or test a
frontend against data the backend does not have: inject a field into an API
response, fake a write endpoint so nothing persists, or reproduce a state the
real data cannot reach.

```bash
web-cli mock rules.py
web-cli mock rules.py --duration 300
```

```python
# rules.py
MATCH = ["/api/"]          # optional: only these URLs are paused at all

def on_request(request):
    # request: {"url", "method", "post_data"}
    # Return None to send it to the network, or answer locally:
    if request["method"] == "PUT" and "/api/profile" in request["url"]:
        return {"status": 200, "body": {"ok": True}}

def on_response(request, status, body):
    # body: decoded text of the real response.
    # Return None to pass it through, or a replacement (dict, str or bytes).
    if "/api/profile" in request["url"]:
        return body.replace('"plan":"free"', '"plan":"pro"')
```

A dict body is JSON-encoded with `content-type: application/json`. Locally
answered responses reflect the request's `Origin`, so cross-origin API calls
pass CORS. Module-level state persists across calls, so `on_response` can
stash a GET and `on_request` can answer a later PUT from it. The full contract
is in the docstring of [`commands/mock.py`](src/web_cli/commands/mock.py).

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Command error, for example a JavaScript exception |
| `3` | Chrome unreachable, and launching it failed |
| `4` | No tab matches the selector |

## How I run it

On my Macs, the debug Chrome is the normal browser. A small launcher app at
`/Applications/Chrome.app` execs Google Chrome with
`--remote-debugging-port=9222` and the `Chrome-Debug` profile. It execs rather
than wraps Chrome, so Chrome's code signature and Keychain access stay intact.
The launcher is registered as the default browser, so every clicked link and
cold start lands on the debuggable profile, and the real `Google Chrome.app` is
hidden. The launcher lives in my dotfiles, not in this repo, and the
auto-launch path in `discovery.py` points at it.

## License

[MIT](LICENSE)
