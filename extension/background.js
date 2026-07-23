// Web-Cli Network Logger - MV3 service worker.
//
// Captures every network request via chrome.webRequest and POSTs completed
// records to the local web-cli server. webRequest is observation-only in
// Manifest V3, so this sees request/response headers, status, timing, and
// metadata - but NOT request/response bodies (a hard MV3 limitation).

const DEFAULT_PORT = 9333;
let PORT = DEFAULT_PORT;
let enabled = false;

const inflight = new Map(); // requestId -> partial record, assembled across events
let buffer = [];            // completed records waiting to be flushed
let flushTimer = null;

function ingestUrl() {
  return `http://127.0.0.1:${PORT}/ingest`;
}

// Don't capture our own POSTs to the local server (avoids a feedback loop).
function isOwn(url) {
  return (
    url.startsWith(`http://127.0.0.1:${PORT}`) ||
    url.startsWith(`http://localhost:${PORT}`)
  );
}

async function loadConfig() {
  const { wcPort, wcEnabled } = await chrome.storage.local.get(["wcPort", "wcEnabled"]);
  if (wcPort) PORT = wcPort;
  enabled = !!wcEnabled;
  updateBadge();
}
loadConfig();

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.wcPort) PORT = changes.wcPort.newValue || DEFAULT_PORT;
  if (changes.wcEnabled) {
    enabled = !!changes.wcEnabled.newValue;
    updateBadge();
  }
});

function updateBadge() {
  chrome.action.setBadgeText({ text: enabled ? "ON" : "" });
  chrome.action.setBadgeBackgroundColor({ color: "#00aa77" });
}

chrome.action.onClicked.addListener(async () => {
  enabled = !enabled;
  await chrome.storage.local.set({ wcEnabled: enabled });
  updateBadge();
});

function rec(details) {
  let r = inflight.get(details.requestId);
  if (!r) {
    r = { requestId: details.requestId };
    inflight.set(details.requestId, r);
  }
  return r;
}

chrome.webRequest.onBeforeRequest.addListener(
  (d) => {
    if (!enabled || isOwn(d.url)) return;
    const r = rec(d);
    r.url = d.url;
    r.method = d.method;
    r.type = d.type;
    r.tabId = d.tabId;
    r.frameId = d.frameId;
    r.initiator = d.initiator;
    r.startTime = d.timeStamp;
    r.requestBody = normalizeBody(d.requestBody);
  },
  { urls: ["<all_urls>"] },
  ["requestBody"]
);

// Turn chrome's requestBody shape into something replayable. formData becomes
// a urlencoded string; raw bytes are decoded as UTF-8 (best effort).
function normalizeBody(rb) {
  if (!rb) return null;
  if (rb.formData) {
    const parts = [];
    for (const k of Object.keys(rb.formData)) {
      for (const v of rb.formData[k]) {
        parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
      }
    }
    return { kind: "form", value: parts.join("&") };
  }
  if (rb.raw?.length) {
    try {
      const dec = new TextDecoder("utf-8", { fatal: false });
      let out = "";
      for (const part of rb.raw) {
        if (part.bytes) out += dec.decode(new Uint8Array(part.bytes), { stream: true });
      }
      out += dec.decode();
      return { kind: "raw", value: out };
    } catch {
      return { kind: "raw", value: null, note: "binary body, not decoded" };
    }
  }
  return null;
}

chrome.webRequest.onSendHeaders.addListener(
  (d) => {
    if (!enabled || isOwn(d.url)) return;
    rec(d).requestHeaders = d.requestHeaders;
  },
  { urls: ["<all_urls>"] },
  ["requestHeaders", "extraHeaders"]
);

chrome.webRequest.onHeadersReceived.addListener(
  (d) => {
    if (!enabled || isOwn(d.url)) return;
    const r = rec(d);
    r.statusCode = d.statusCode;
    r.statusLine = d.statusLine;
    r.responseHeaders = d.responseHeaders;
  },
  { urls: ["<all_urls>"] },
  ["responseHeaders", "extraHeaders"]
);

chrome.webRequest.onCompleted.addListener(
  (d) => {
    if (!enabled || isOwn(d.url)) return;
    const r = rec(d);
    r.statusCode = d.statusCode;
    r.fromCache = d.fromCache;
    r.ip = d.ip;
    r.endTime = d.timeStamp;
    finish(d.requestId);
  },
  { urls: ["<all_urls>"] }
);

chrome.webRequest.onErrorOccurred.addListener(
  (d) => {
    if (!enabled || isOwn(d.url)) return;
    const r = rec(d);
    r.error = d.error;
    r.fromCache = d.fromCache;
    r.endTime = d.timeStamp;
    finish(d.requestId);
  },
  { urls: ["<all_urls>"] }
);

function finish(requestId) {
  const r = inflight.get(requestId);
  if (!r) return;
  inflight.delete(requestId);
  r.durationMs =
    r.startTime != null && r.endTime != null
      ? Math.round(r.endTime - r.startTime)
      : null;
  r.ts = r.endTime;
  buffer.push(r);
  scheduleFlush();
}

function scheduleFlush() {
  if (flushTimer) return;
  flushTimer = setTimeout(flush, 250);
}

async function flush() {
  flushTimer = null;
  if (!buffer.length) return;
  const batch = buffer;
  buffer = [];
  try {
    await fetch(ingestUrl(), {
      method: "POST",
      // text/plain keeps this a CORS "simple request" - no preflight.
      headers: { "Content-Type": "text/plain" },
      body: JSON.stringify(batch),
    });
  } catch {
    // Server not running - drop this batch rather than growing unbounded.
  }
}
