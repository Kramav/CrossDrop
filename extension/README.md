# CrossDrop browser extension

Right-click a page, pick **Send page to Living Room**, and the wall shows it.
Chrome and Edge (Manifest V3). No build step, no dependencies. It calls
`POST /v1/navigate` and `GET /v1/status` and nothing else, so the agent needs
no change and no CORS middleware.

## Install

Not on a web store yet, so load it unpacked. Once per browser:

1. Open `chrome://extensions` (Edge: `edge://extensions`).
2. Turn on **Developer mode**.
3. **Load unpacked** → pick `E:\Company\Github\CrossDrop\extension`.
4. Pin it: puzzle-piece icon → pin **CrossDrop**.

After a `git pull`, press the reload arrow on its card.

## Set up a display

Click the icon. **Display** opens by itself the first time.

| Field | What goes in |
|---|---|
| Name | What the menu says: *Send page to* **Living Room** |
| Address | `http://<pi-tailnet-ip>:8080`, same as `url` in `roomctl/targets.toml` |
| Token | same as `token` in `targets.toml`, or `sudo cat /etc/crossdrop/token` on the Pi |

**Save and test** asks the browser for access to that one address, then reads
`/v1/status` and says *Connected to Living Room: chromium, 2 screens*. Anything
else it prints is the reason, such as a wrong token, the Pi being off, or its
browser being down.

Access is granted per address because Chrome's permission patterns cannot say
"the tailnet". The alternative was access to every website at install time.

## Use

- **Right-click** a page → *Send page to …*, or a link → *Send link to …*
- **<kbd>Alt</kbd>+<kbd>Shift</kbd>+<kbd>D</kbd>** sends the current tab. Change
  it at `chrome://extensions/shortcuts`.
- **The popup** → *Send this tab*.

The badge says how it went: a green **✓** that clears itself, or a red **!**
that stays until you open the popup. Hover the icon to read why.

Worth knowing:

- **It sends the address, not the page.** The wall loads it fresh in its own
  browser, so a page you are logged into shows up logged out.
- **http and https only.** `chrome://`, `edge://` and `file://` pages are
  refused with a message. The agent allows nothing else.
- **First screen only** for now. Choosing a screen and more than one display is
  M2 ([NEXT-STEPS.md](../NEXT-STEPS.md)).
- **The token syncs** with your browser profile (`chrome.storage.sync`), so
  another PC signed into the same profile has it too. On a tailnet that is the
  intended trust boundary (PLAN.md §11).

## Freethrow

[Freethrow](https://github.com/kramav/Freethrow) throws windows by gesture. To
send a *browser* window to a display it needs that window's URL, and Windows
only gives it a title bar. The **Freethrow** switch in the popup fills that gap.
It is off by default. Turning it on asks for the `tabs` permission, and turning
it off gives that permission back.

While on, the extension tells a listener on this PC what every normal browser
window is showing. Nothing leaves the machine, and private windows are never
included.

### Contract (v1)

```
PUT http://127.0.0.1:47800/crossdrop/windows
Content-Type: application/json
Origin: chrome-extension://<extension id>

{
  "v": 1,
  "instance": "<uuid, one per browser profile>",
  "browser": "chrome" | "edge",
  "at": 1789000000000,
  "windows": [
    {"id": 1, "focused": true, "state": "normal",
     "left": 0, "top": 0, "width": 1280, "height": 800,
     "title": "Slides - Google Docs", "url": "https://docs.google.com/..."}
  ]
}
```

- **Each PUT is the whole picture for one `instance`.** Replace that instance's
  snapshot and don't merge. A window missing from it has closed. Two browser
  profiles send two instances.
- **When:** on every tab or window change, debounced by 250 ms, and once a
  minute, so a listener started late catches up within a minute.
- **`title` and `url` are the window's active tab.**
- **Bounds are in DIPs** as `chrome.windows` reports them. DWM reports physical
  pixels, so divide those by the monitor's scale before comparing. This is
  believed, not measured.

What the listener has to do:

- **Bind `127.0.0.1` only**, and answer only `PUT /crossdrop/windows`, with 204.
- **Refuse any request whose `Origin` does not start with
  `chrome-extension://`.** A web page cannot forge that header. Its PUT with a
  JSON body also needs a CORS preflight, which the listener never answers.
- **Match a window handle by bounds first, title second.** Chrome's title bar is
  `<tab title> - Google Chrome`. Edge adds the profile name and "and N more
  pages", so its titles do not match exactly.

## Tests

From the repo root:

```powershell
python -m pytest tests/test_extension.py                        # manifest only
$env:CROSSDROP_SMOKE = "1"; $env:CROSSDROP_BROWSER = "chromium" # or "edge"
python -m pytest tests/test_extension.py                        # real headless browser
```

The second one loads the extension into a headless Chrome or Edge and drives it
against a real agent: it sends a URL, reads the errors, and receives a bridge
PUT. It takes about 15 seconds, and CI runs it in the smoke job.
