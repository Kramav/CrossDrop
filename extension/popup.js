// The popup is UI only. Every request to a display goes through the service
// worker (background.js), so there is one fetch path and one error vocabulary.

const $ = s => document.querySelector(s);

function say(el, ok, text) {
  el.textContent = text;
  el.className = "msg" + (ok === true ? " ok" : ok === false ? " bad" : "");
}

async function render() {
  const { devices = [] } = await chrome.storage.sync.get("devices");
  const d = devices[0];
  $("#title").textContent = d ? `Send to ${d.name}` : "CrossDrop";
  $("#send").disabled = !d;
  $("#setup").open = !d;
  if (d) {
    $("#name").value = d.name;
    $("#url").value = d.url;
    $("#token").value = d.token;
  }
  const { freethrow } = await chrome.storage.local.get("freethrow");
  $("#freethrow").checked = !!freethrow;
}

$("#send").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  say($("#send-msg"), null, "Sending…");
  const r = await chrome.runtime.sendMessage({ type: "send", url: tab && tab.url });
  say($("#send-msg"), r.ok, r.message);
});

$("#save").addEventListener("click", async () => {
  const msg = $("#setup-msg");
  let u;
  try {
    u = new URL($("#url").value.trim());
  } catch {
    return say(msg, false, "That address doesn't parse. It looks like http://100.x.y.z:8080");
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") {
    return say(msg, false, "The address has to start with http:// or https://");
  }
  const token = $("#token").value.trim();
  if (!token) return say(msg, false, "Paste the display's token.");

  // First await in the handler: a permission request has to ride the click.
  const granted = await chrome.permissions.request({ origins: [`${u.protocol}//${u.hostname}/*`] });
  if (!granted) return say(msg, false, `CrossDrop can't reach ${u.host} without that permission.`);

  const name = $("#name").value.trim() || u.hostname;
  await chrome.storage.sync.set({ devices: [{ name, url: u.origin, token }] });
  say(msg, null, "Testing…");
  const r = await chrome.runtime.sendMessage({ type: "check" });
  say(msg, r.ok, r.message);
  render();
});

$("#freethrow").addEventListener("change", async e => {
  if (e.target.checked) {
    // Reading every window's address needs "tabs", which is why it is optional.
    const granted = await chrome.permissions.request({ permissions: ["tabs"], origins: ["http://127.0.0.1/*"] });
    if (!granted) { e.target.checked = false; return; }
    await chrome.storage.local.set({ freethrow: true });
  } else {
    await chrome.storage.local.set({ freethrow: false });
    await chrome.permissions.remove({ permissions: ["tabs"] });
  }
});

(async () => {
  // Opening the popup is how a failure badge gets read, so it is also how it clears.
  chrome.action.setBadgeText({ text: "" });
  chrome.action.setTitle({ title: "CrossDrop" });
  const { last } = await chrome.storage.session.get("last");
  if (last && !last.ok) say($("#send-msg"), false, last.message);
  render();
})();
