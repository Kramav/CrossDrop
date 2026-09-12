// Tells Freethrow (github.com/kramav/Freethrow), running on this PC, what
// each browser window is showing. Win32 hands Freethrow a window handle and a
// title bar, never a URL, so without this it cannot throw a browser window to
// a display. Off until switched on in the popup; the contract is in
// extension/README.md.
//
// Push, not pull: Freethrow is a separate process and cannot call into an
// extension. Native messaging was the alternative and is strictly more parts --
// Chrome launches a fresh host per connection, so it would still need a bridge
// to the Freethrow that is already running.

const FREETHROW_URL = "http://127.0.0.1:47800/crossdrop/windows";
const FREETHROW_DEBOUNCE_MS = 250;  // tabs.onUpdated fires several times per page load

let freethrowTimer;

async function freethrowPush() {
  let { freethrow, instance } = await chrome.storage.local.get(["freethrow", "instance"]);
  if (!freethrow) return;
  // Each browser profile runs its own copy of this extension, and every PUT is
  // a whole snapshot. Without an id, two profiles would erase each other's windows.
  if (!instance) {
    instance = crypto.randomUUID();
    await chrome.storage.local.set({ instance });
  }
  const wins = await chrome.windows.getAll({ populate: true, windowTypes: ["normal"] });
  const windows = wins
    .filter(w => !w.incognito)  // never report a private window, even if allowed to run there
    .map(w => {
      const tab = w.tabs.find(t => t.active) || {};
      return {
        id: w.id, focused: w.focused, state: w.state,
        // DIPs, not the physical pixels DWM reports.
        left: w.left, top: w.top, width: w.width, height: w.height,
        title: tab.title || "", url: tab.url || "",
      };
    });
  try {
    await fetch(FREETHROW_URL, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        v: 1,
        instance,
        browser: navigator.userAgent.includes("Edg/") ? "edge" : "chrome",
        at: Date.now(),
        windows,
      }),
    });
  } catch {
    // Freethrow is not running. That is the normal case, not an error.
  }
}

function freethrowSoon() {
  clearTimeout(freethrowTimer);
  freethrowTimer = setTimeout(freethrowPush, FREETHROW_DEBOUNCE_MS);
}

// Top level, so these wake the service worker. With the switch off each wake
// is one storage read.
for (const event of [
  chrome.tabs.onActivated, chrome.tabs.onUpdated, chrome.tabs.onRemoved,
  chrome.windows.onCreated, chrome.windows.onRemoved,
  chrome.windows.onFocusChanged, chrome.windows.onBoundsChanged,
]) event.addListener(freethrowSoon);

// A Freethrow started after the last tab event would otherwise wait for the
// next one. Once a minute bounds that.
chrome.alarms.onAlarm.addListener(a => { if (a.name === "freethrow") freethrowPush(); });

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes.freethrow) return;
  if (changes.freethrow.newValue) {
    chrome.alarms.create("freethrow", { periodInMinutes: 1 });
    freethrowPush();
  } else {
    chrome.alarms.clear("freethrow");
  }
});
