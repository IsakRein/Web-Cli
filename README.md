# web-cli

Stream **every network request** from your normal, already-running Chrome to your
terminal - no relaunch, no `--remote-debugging-port`, no throwaway "debug"
profile. A Manifest V3 extension captures requests via `chrome.webRequest` and
POSTs them to a tiny local Node server that prints the live log.

## What it can and can't see

Captured: URL, method, resource type, status, request + response headers
(including `Cookie` / `Authorization` via `extraHeaders`), timing, cache/IP,
errors, and the **request** body (form data and raw payloads).

Not captured: **response bodies**. `chrome.webRequest` is observation-only and
never exposes response payloads, in any manifest version - it's built around the
request lifecycle, not the bytes. To read a response body you either:

- **replay the request** - `--curl` prints a ready-to-run curl per request with
  the captured headers/body. Solid for `GET`s (idempotent, and auth replays as
  you). Caveats: re-running non-idempotent methods repeats their side effects;
  single-use/signed/time-boxed requests may be expired; the body can drift from
  what was logged; and bot-protection (Cloudflare/Akamai) fingerprints TLS/HTTP2,
  so plain curl may get challenged.
- or use a `chrome.debugger` capture mode (CDP `Network.getResponseBody`) - not
  built yet; the clean upgrade when you need true bodies with no replay.

## Setup (one time)

1. Load the extension:
   - `chrome://extensions` -> enable **Developer mode** -> **Load unpacked**
   - select the folder printed by `web-cli path`
2. (optional) `npm link` or add `bin/web-cli.js` to your PATH as `web-cli`.
   Otherwise run it as `node bin/web-cli.js ...`.

## Use

```bash
web-cli listen                 # start server, print live log
web-cli listen --curl          # also print a replayable curl under each request
web-cli listen --json          # NDJSON output (pipe to jq / a file)
web-cli listen --type xmlhttprequest --url /api/   # filter
```

Then click the extension's toolbar button so its badge reads **ON**, and browse.
Turn it **OFF** with the same button when done.

### listen options

| flag | meaning |
|---|---|
| `--port N` | server port (default 9333; must match the extension's Options page) |
| `--curl` | print a ready-to-run curl under each request |
| `--json` | raw NDJSON instead of pretty lines |
| `--out FILE` | also append every record as NDJSON to FILE |
| `--type T` | filter by resource type (`xmlhttprequest`, `script`, `image`, `main_frame`, ...) |
| `--method M` | filter by HTTP method |
| `--url SUBSTR` | filter by URL substring (case-insensitive) |
| `--tab ID` | filter by Chrome tab id |
| `--no-color` | disable ANSI colors |

If you change `--port`, set the same port in the extension's Options page
(`chrome://extensions` -> web-cli -> Details -> Extension options).

## How it fits together

```
Chrome (your normal profile)
  └─ extension/ (webRequest listeners, toggled by toolbar button)
        └─ POST batches ──> http://127.0.0.1:<port>/ingest
                                └─ bin/web-cli.js  (prints / filters / --curl / --json)
```
