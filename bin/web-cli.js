#!/usr/bin/env node

// web-cli - local server + live printer for the Web-Cli Network Logger extension.
// Zero dependencies (Node built-ins only).

const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");

const DEFAULT_PORT = 9333;

function parseArgs(argv) {
  const opts = {
    port: DEFAULT_PORT,
    json: false,
    curl: false,
    color: process.stdout.isTTY && !process.env.NO_COLOR,
    type: null,
    method: null,
    url: null,
    tab: null,
    out: null,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = () => argv[++i];
    switch (a) {
      case "--port": opts.port = parseInt(next(), 10); break;
      case "--json": opts.json = true; break;
      case "--curl": opts.curl = true; break;
      case "--no-color": opts.color = false; break;
      case "--type": opts.type = next(); break;
      case "--method": opts.method = next().toUpperCase(); break;
      case "--url": opts.url = next().toLowerCase(); break;
      case "--tab": opts.tab = parseInt(next(), 10); break;
      case "--out": opts.out = next(); break;
      default:
        console.error(`unknown option: ${a}`);
        process.exit(1);
    }
  }
  return opts;
}

const C = {
  reset: "\x1b[0m", dim: "\x1b[2m", bold: "\x1b[1m",
  red: "\x1b[31m", green: "\x1b[32m", yellow: "\x1b[33m",
  blue: "\x1b[34m", magenta: "\x1b[35m", cyan: "\x1b[36m", gray: "\x1b[90m",
};

function colorFor(opts, code, s) {
  return opts.color ? code + s + C.reset : s;
}

function statusColor(status) {
  if (status == null) return C.magenta;
  if (status >= 500) return C.red;
  if (status >= 400) return C.yellow;
  if (status >= 300) return C.cyan;
  return C.green;
}

function fmtTime(ts) {
  const d = ts != null ? new Date(ts) : new Date();
  const p = (n, l = 2) => String(n).padStart(l, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}

function fmtDuration(ms) {
  if (ms == null) return "-";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

function pad(s, n) {
  s = String(s == null ? "" : s);
  return s.length >= n ? s : s + " ".repeat(n - s.length);
}

// Shell-quote for POSIX sh: wrap in single quotes, escape embedded quotes.
function shq(s) {
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}

// Reconstruct a runnable curl from a captured request. Headers include the
// captured Cookie/Authorization (extraHeaders), so it replays as you. Safe for
// GETs; replaying non-idempotent methods re-runs their side effects.
function buildCurl(r) {
  const parts = ["curl", "--compressed"];
  if (r.method && r.method !== "GET") parts.push("-X", r.method);
  const skip = new Set(["content-length", "host"]);
  for (const h of r.requestHeaders || []) {
    if (!h.name || h.name.startsWith(":")) continue; // drop HTTP/2 pseudo-headers
    if (skip.has(h.name.toLowerCase())) continue;
    parts.push("-H", shq(`${h.name}: ${h.value}`));
  }
  if (r.requestBody?.value) {
    parts.push("--data-raw", shq(r.requestBody.value));
  }
  parts.push(shq(r.url));
  return parts.join(" ");
}

function passesFilters(r, opts) {
  if (opts.type && r.type !== opts.type) return false;
  if (opts.method && r.method !== opts.method) return false;
  if (opts.tab != null && r.tabId !== opts.tab) return false;
  if (opts.url && !(r.url || "").toLowerCase().includes(opts.url)) return false;
  return true;
}

function printRecord(r, opts, outStream) {
  if (opts.json) {
    const line = JSON.stringify(r);
    process.stdout.write(line + "\n");
    if (outStream) outStream.write(line + "\n");
    return;
  }
  const status = r.error ? r.error : (r.statusCode != null ? r.statusCode : "?");
  const statusStr = colorFor(opts, statusColor(r.error ? null : r.statusCode), pad(status, 6));
  const method = colorFor(opts, C.bold, pad(r.method || "?", 6));
  const type = colorFor(opts, C.gray, pad(r.type || "?", 13));
  const dur = colorFor(opts, C.dim, pad(fmtDuration(r.durationMs), 7));
  const time = colorFor(opts, C.dim, fmtTime(r.ts));
  const cache = r.fromCache ? colorFor(opts, C.blue, " (cache)") : "";
  const line = `${time}  ${method} ${statusStr} ${type} ${dur} ${r.url || ""}${cache}`;
  console.log(line);
  if (opts.curl) console.log(colorFor(opts, C.dim, `  ${buildCurl(r)}`));
  if (outStream) outStream.write(`${JSON.stringify(r)}\n`);
}

function cmdListen(opts) {
  const outStream = opts.out ? fs.createWriteStream(opts.out, { flags: "a" }) : null;
  let count = 0;

  const server = http.createServer((req, res) => {
    // CORS: allow the extension service worker to POST here.
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Access-Control-Allow-Methods", "POST, GET, OPTIONS");
    res.setHeader("Access-Control-Allow-Headers", "Content-Type");

    if (req.method === "OPTIONS") { res.writeHead(204); res.end(); return; }

    if (req.method === "GET" && req.url === "/") {
      res.writeHead(200, { "Content-Type": "text/plain" });
      res.end(`web-cli listening, ${count} requests captured\n`);
      return;
    }

    if (req.method === "POST" && req.url === "/ingest") {
      let body = "";
      req.on("data", (chunk) => (body += chunk));
      req.on("end", () => {
        try {
          const batch = JSON.parse(body);
          for (const r of batch) {
            if (!passesFilters(r, opts)) continue;
            count++;
            printRecord(r, opts, outStream);
          }
        } catch {
          // ignore malformed batch
        }
        res.writeHead(200); res.end("ok");
      });
      return;
    }

    res.writeHead(404); res.end();
  });

  server.on("error", (e) => {
    if (e.code === "EADDRINUSE") {
      console.error(`port ${opts.port} is already in use. Pick another with --port N (and set it in the extension options).`);
    } else {
      console.error(e.message);
    }
    process.exit(1);
  });

  server.listen(opts.port, "127.0.0.1", () => {
    if (!opts.json) {
      console.error(`web-cli listening on http://127.0.0.1:${opts.port}`);
      console.error(`Load the extension, click its toolbar button so the badge reads ON, then browse.`);
      console.error(`Waiting for requests... (Ctrl-C to stop)\n`);
    }
  });
}

function cmdPath() {
  console.log(path.resolve(__dirname, "..", "extension"));
}

function help() {
  console.log(`web-cli - stream all Chrome network requests to your terminal

USAGE
  web-cli listen [options]     start the local server and print the live network log
  web-cli path                 print the extension directory to load in Chrome
  web-cli help                 show this help

LISTEN OPTIONS
  --port N        server port (default ${DEFAULT_PORT}; must match extension options)
  --json          output raw NDJSON (one JSON record per line) instead of pretty lines
  --curl          print a ready-to-run curl (with captured headers/body) under each request
  --out FILE      also append every record as NDJSON to FILE
  --type T        only show this resource type (xmlhttprequest, script, image, main_frame, ...)
  --method M      only show this HTTP method (GET, POST, ...)
  --url SUBSTR    only show URLs containing SUBSTR (case-insensitive)
  --tab ID        only show requests from this Chrome tab id
  --no-color      disable ANSI colors

ONE-TIME SETUP
  1. chrome://extensions -> enable "Developer mode" -> "Load unpacked"
     -> select the folder printed by:  web-cli path
  2. Run:  web-cli listen
  3. Click the extension's toolbar button so its badge reads ON.
  4. Browse. Requests stream into the terminal.

NOTE
  This uses chrome.webRequest: URLs, methods, status, headers, and timing are
  captured, but NOT response bodies (a Manifest V3 limitation).`);
}

function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  switch (cmd) {
    case "listen": cmdListen(parseArgs(rest)); break;
    case "path": cmdPath(); break;
    case undefined:
    case "help":
    case "-h":
    case "--help": help(); break;
    default:
      console.error(`unknown command: ${cmd}\n`);
      help();
      process.exit(1);
  }
}

main();
