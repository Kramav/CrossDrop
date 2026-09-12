// The extension's service worker: every request to a display is made here.
// The popup, the context menu and the shortcut all end in send().
//
// It calls POST /v1/navigate and GET /v1/status and nothing else. The agent
// has no CORS middleware and needs none: a service worker holding a host
// permission for the display's origin skips the preflight entirely (verified
// on Chrome 152 and Edge 153 against a real Pi; tests/test_extension.py keeps
// it verified). Without the permission the preflight 405s, so every request
// checks for it first and says so, instead of reporting "unreachable".
//
// Host permissions are optional and requested per display when it is saved.
// A match pattern cannot express 100.64.0.0/10, so the alternative was
// "http://*/*" at install -- a warning that this extension reads every site.

importScripts("freethrow.js");

const TIMEOUT_MS = 20000;  // navigate wakes the panel first; a few seconds is normal

// ponytail: one display until M2's picker. Stored as a list already, so M2
// adds entries rather than migrating the schema (PLAN.md §13 decision 3).
async function device() {
  const { devices = [] } = await chrome.storage.sync.get("devices");
  return devices[0];
}

// Ports are ignored by match patterns, so one grant covers the host.
function originPattern(url) {
  const u = new URL(url);
  return `${u.protocol}//${u.hostname}/*`;
}

// The same vocabulary as roomctl's typed errors, so a failure reads the same
// from the badge, the popup, the tray and the CLI.
function explain(status, detail, name) {
  switch (status) {
    case 401: return `${name} rejected the token. Check it in the popup.`;
    case 404: return `${name}: ${detail || "not found"}.`;
    case 422: return `${name} refused that URL.`;
    case 501: return `${name}'s browser can't do that.`;
    case 503: return `${name} is up, but its browser isn't${detail ? ": " + detail : "."}`;
    default:  return `${name} answered ${status}${detail ? ": " + detail : "."}`;
  }
}

async function call(d, path, init = {}) {
  if (!await chrome.permissions.contains({ origins: [originPattern(d.url)] })) {
    return { ok: false, message: `No access to ${new URL(d.url).host} yet. Open the popup and press Save.` };
  }
  let r;
  try {
    r = await fetch(d.url + path, {
      ...init,
      headers: { Authorization: "Bearer " + d.token, "Content-Type": "application/json" },
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (e) {
    return {
      ok: false,
      message: e.name === "TimeoutError"
        ? `${d.name} didn't answer within ${TIMEOUT_MS / 1000} s.`
        : `Can't reach ${d.name}. Is it on, and is Tailscale connected?`,
    };
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const detail = typeof body.detail === "string" ? body.detail : "";
    return { ok: false, message: explain(r.status, detail, d.name) };
  }
  return { ok: true, body };
}

async function send(url) {
  const d = await device();
  let result;
  if (!d) {
    result = { ok: false, message: "No display set up yet. Open the popup to add one." };
  } else if (!/^https?:\/\//.test(url || "")) {
    // /v1/navigate allows http and https only (PLAN.md §10).
    const scheme = (url || "").split(":")[0] || "this";
    result = { ok: false, message: `Can't send ${scheme}: pages. Only http and https work.` };
  } else {
    const r = await call(d, "/v1/navigate", { method: "POST", body: JSON.stringify({ url }) });
    result = r.ok ? { ok: true, message: `Sent to ${d.name}.` } : r;
  }
  await report(result);
  return result;
}

// What the popup shows when saving: proves the url and the token together.
async function check() {
  const d = await device();
  if (!d) return { ok: false, message: "No display set up yet." };
  const r = await call(d, "/v1/status");
  if (!r.ok) return r;
  const s = r.body;
  const screens = (s.screens || []).length;
  // `up` is always true (a frozen wart); `browser` and `error` carry the news.
  if (s.browser !== "ok") return { ok: false, message: `${d.name} is up, but its browser isn't: ${s.error || s.browser}` };
  return { ok: true, message: `Connected to ${d.name}: ${s.kind}, ${screens} screen${screens === 1 ? "" : "s"}.` };
}

// Where the user is looking: the toolbar. A failure keeps its badge until the
// popup is opened, because the hover title is the only place that says why.
async function report(result) {
  await chrome.storage.session.set({ last: { ...result, at: Date.now() } });
  await chrome.action.setBadgeBackgroundColor({ color: result.ok ? "#188038" : "#d93025" });
  await chrome.action.setBadgeText({ text: result.ok ? "✓" : "!" });
  await chrome.action.setTitle({ title: `CrossDrop: ${result.message}` });
  if (result.ok) setTimeout(() => chrome.action.setBadgeText({ text: "" }), 3000);
}

async function menus() {
  const d = await device();
  const to = d ? d.name : "display";
  await chrome.contextMenus.removeAll();
  chrome.contextMenus.create({ id: "send-page", title: `Send page to ${to}`, contexts: ["page"] });
  chrome.contextMenus.create({ id: "send-link", title: `Send link to ${to}`, contexts: ["link"] });
}

chrome.runtime.onInstalled.addListener(menus);
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "sync" && changes.devices) menus();
});

chrome.contextMenus.onClicked.addListener(info => {
  send(info.menuItemId === "send-link" ? info.linkUrl : info.pageUrl);
});

chrome.commands.onCommand.addListener((command, tab) => {
  if (command === "send-tab") send(tab && tab.url);
});

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg.type === "send") send(msg.url).then(reply);
  else if (msg.type === "check") check().then(reply);
  else return false;
  return true;  // reply arrives asynchronously
});
