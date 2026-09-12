# CrossDrop

Drive a keyboard-less room display from any box on the tailnet: point it at a
URL, or drop a file on it and have it render. The display is a Raspberry Pi or
any Debian box with monitors on it — a Proxmox host, say.

- **Pi setup** — [deploy/pi/pi-setup.md](deploy/pi/pi-setup.md) (OS) then
  [deploy/pi/README.md](deploy/pi/README.md) (agent). One script does both:
  [deploy/pi/setup.sh](deploy/pi/setup.sh).
- **Updating a Pi that's already running** —
  [deploy/pi/update-over-ssh.md](deploy/pi/update-over-ssh.md). The runbook:
  ship a tag, change a setting, verify it, roll it back.
- **Debian / Proxmox host setup** — [deploy/linux.md](deploy/linux.md). Same
  script, other branch.
- **Build plan and phases** — [PLAN.md](PLAN.md).
- **What's open and what's next** — [NEXT-STEPS.md](NEXT-STEPS.md), the living
  roadmap.

## Architecture

One server, many clients. The Pi runs **one FastAPI process** that owns a kiosk
browser and the monitors; everything else in this repo is a client of its frozen
`/v1` HTTP API (contract table near the bottom of this file). Adding a control
surface never touches the server.

```
tray app  ─┐
web UI    ─┼─ HTTP /v1 (bearer token, over tailnet) ─→  agent/app.py
roomctl   ─┘                                              │
eve (imports roomctl as a library) ──────────────────┘    │
                                    ┌─────────────────────┼─────────────────┐
                              browser.py             display.py        storage.py
                          CDP / WebDriver BiDi      xrandr + DPMS     tmpfs uploads
                          over a websocket          (monitor power)   (RAM, capped)
                                    ↓                    ↓
                            kiosk browser window    the physical monitors
```

Two facts explain most of the code. **The Pi has no keyboard**, so anything that
blanks the screen and wakes only on input is unrecoverable — hence explicit
power control in `display.py` and the self-rolling-back updater. **The browser is
driven remotely**, over CDP for Chromium/Edge and WebDriver BiDi for Firefox;
scroll and media need CDP, so Firefox gets `501` on those.

A **target** is a Pi. A **screen** is one kiosk window on it — usually a
monitor, optionally half of one.

### Where things live

Each file's own docstring is the detailed version — the point of this table is
that you only have to open one.

| Path | What it is |
|---|---|
| [agent/app.py](agent/app.py) | The whole HTTP surface: routes, pydantic models, auth, config loading. The only file that defines the API. |
| [agent/browser.py](agent/browser.py) | Launches the kiosk browser, drives the tab. CDP *and* BiDi over one JSON-RPC helper. Navigate, scroll, media, window placement. |
| [agent/display.py](agent/display.py) | Monitor power only, via `xrandr`/DPMS. Idle watcher; every screen-touching route wakes the display first. X11 only. |
| [agent/storage.py](agent/storage.py) | Upload store. Extension allowlist, size cap, keep-newest-N sweep. It's tmpfs, so it's RAM. |
| [agent/settings.py](agent/settings.py) | The subset of config the agent may rewrite at runtime — screen names, home URLs, the window mode, which monitors are split → `settings.json`. Everything that decides *what runs* stays file-only in `config.toml`; `SAFE_KEYS` is the whole list. |
| [agent/selfcheck.py](agent/selfcheck.py) | `python -m agent selfcheck` — in-process boot check that gates a release swap. |
| [agent/config.example.toml](agent/config.example.toml) | Install-time config: browser kind, ports, paths. No secret — the token is its own file, `/etc/crossdrop/token`. Real one is git-ignored. |
| [roomctl/__init__.py](roomctl/__init__.py) | The client library — one function per route. `eve` imports this. |
| [roomctl/cli.py](roomctl/cli.py) | argparse shell over the above; prints the agent's JSON verbatim. |
| [web/index.html](web/index.html) | The controller UI the agent serves at `/`. Single file, no build step, no framework. |
| [web/home.html](web/home.html) | The idle screen the kiosk sits on. Also single-file. |
| [deploy/pi/](deploy/pi/) | Provisioning (`setup.sh` — Pi *and* plain Debian), systemd units, tmpfs profile snapshots, and `update.sh` — the release-gated auto-updater with rollback. |
| [deploy/pi/update-over-ssh.md](deploy/pi/update-over-ssh.md) | The runbook for a Pi already on the wall: ship a tag, flip a setting, verify, roll back, read the logs before they're gone. |
| [deploy/pi/smoke-on-the-pi.md](deploy/pi/smoke-on-the-pi.md) | Driving the real browser on the real box: what the smoke suite proves, why the agent has to be stopped first, and what each failure means. |
| [deploy/linux.md](deploy/linux.md) | Running the display on a Debian box instead of a Pi, and why it goes on the Proxmox host rather than in a guest. |
| [deploy/windows/roomtray.ps1](deploy/windows/roomtray.ps1) | The tray client. Pure PowerShell + WinForms so it runs on a box with no checkout and no Python. |
| [tests/](tests/) | pytest, one file per surface. No browser needed unless `CROSSDROP_SMOKE=1`. |
| [PLAN.md](PLAN.md) | Why it's built this way, phase by phase. Section numbers referenced from code comments. §11 holds the adversarial review's standing decisions and what is still unverified on hardware; §13 is the product roadmap from here. |
| [NEXT-STEPS.md](NEXT-STEPS.md) | **Living.** Where the project stands, what is open, what is next. The one file to read first. |
| [DEBT.md](DEBT.md) | Review findings deliberately *not* fixed, each with the trigger that would change the answer. |

## Controlling a display

Three ways, same frozen API underneath.

**Tray app (Windows)** — [deploy/windows/roomtray.ps1](deploy/windows/roomtray.ps1).
Copy a link or a file, double-click the tray icon, it's on the wall. The icon
colour is the display's state: blue awake, grey asleep, red unreachable.
Right-click for screen, Home, Reload, Display off. Reads the same
`targets.toml`; no install, no dependencies. See
[deploy/windows/README.md](deploy/windows/README.md).

```powershell
powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File deploy\windows\roomtray.ps1
```

**Web UI** — the controller the agent serves, and the only place that shows you
the wall rather than describing it. Open it as its own window:

```powershell
msedge.exe --app=http://<pi-tailnet-ip>:8080/
```

```
┌ Acer │ Samsung │ Both ──  https://… ──[Show]──  Home  Reload  ⋮ ┐
│ ┌──────────────────────────────────────────────┐  ┌────────┐   │
│ │ ■ ACER · 2545×1440 · 8:13:13 PM          ⟳  │  │ SCROLL │   │
│ │                                              │  │ ⤒ ▲▼ ⤓ │   │
│ │            the capture, full-bleed           │  │   ⏬   │   │
│ │                                              │  │ WINDOW │   │
│ │       ┌ ⏪ ⏸ ⏩ 4:12/9:30 🔊 ──▭─ ┐         │  │  ⊟ ⛶   │   │
│ └───────┴─────────────────────────────┴────────┘  │DISPLAY◐│   │
│ [Type into the focused field…] □hide [Send]       ↑↓ PgDn ⌃K   │
└────────────────────────────────────────────────────────────────┘
```

- **The capture is the page.** Drop a file or paste a link anywhere on it
  (`.pdf .png .jpg .jpeg .gif .webp .txt`, plus `.mp4 .webm .mp3 .m4a .wav`,
  25 MB cap) and the display follows. Press **⟳** for a fresh one; it is never
  on a timer.
- **Click the picture to click the page**, when the agent has `[interact]`
  enabled. The badge says so in words when it is armed.
- **The rail** is everything that moves the page without replacing it: scroll,
  auto-scroll and its speed, set the kiosk aside, display power.
- **The dock** appears over the capture by itself whenever the screen actually
  has a video or audio on it, and goes away again when it does not.
- **<kbd>Ctrl</kbd><kbd>K</kbd>** is everything else — saved links, send a file,
  settings — as one list you filter by typing. The **⋮** button opens the same
  thing.

Paste the agent token into **Settings** on first run; it stays in that browser.

Both the web UI and the tray hide what the agent's browser can't do, from
`supports` in `/v1/status` — on a Firefox agent the scroll rail, the playback
dock, the capture and the screen picker are simply absent, with one line saying
why, rather than present and returning 501. An agent too old to report
`supports` gets the old behaviour: everything shown.

**Settings** also holds the screens editor — each monitor's name and home URL,
applied live with no restart. Above the rows is a scale map of the monitors
drawn where `xrandr` reports them, numbered to match the rows: screens are named
left to right, and the map is what tells you which row is the panel on the left.

The *arrangement* is not editable, by design. Position and size are read from
`xrandr` at every load and never saved, so they cannot go stale — a layout saved
against one set of monitors used to win over the set actually attached, and put
both kiosk windows on one screen. Rearranging monitors is the session's job
(`xrandr`, or the desktop's own display settings).

What **is** editable is how many screens a panel is cut into. **Split** a
monitor left/right or top/bottom and each half becomes an ordinary screen with
its own name, home page, captures and input — two monitors split gives four. The
halves are derived from `xrandr` too, so they follow a monitor that changes.

Splitting needs the **fullscreen** window mode, which the same panel toggles: a
`--kiosk` window is entitled to refuse half-screen bounds, and two halves that
both came up fullscreen would sit on top of each other. Choosing a split
switches the mode with it.

Neither applies on click. Both change how the browser was *launched*, so they
stage as a proposal — the map previews the result in dashed outline — and
**Confirm choices** saves them and relaunches the browser. That costs the wall
15–30 seconds of black; each screen comes back to what it was showing. There is
no way to make it cheaper, which is why it is behind a confirmation.

Edits persist to `~/.local/share/crossdrop/settings.json`, which the agent
owns and `update.sh` never touches. They do **not** go into
`/etc/crossdrop/config.toml` — it holds the bearer token and is deliberately
`root:<user> 640`, so the agent cannot write it. The token, `browser.kind`,
`profile_dir`, `upload.dir`, `extensions_dir` and `debug_port` stay file-only for the same reason:
they are install-time facts that need a browser relaunch, not a config reload.

**`roomctl`** — the CLI, and the same functions eve imports.

From the repo root on a controller box, not on the Pi:

```sh
pip install -e .                                  # puts `roomctl` on PATH
cp roomctl/targets.example.toml roomctl/targets.toml
$EDITOR roomctl/targets.toml                      # the Pi's 100.x address + token

roomctl status
roomctl navigate https://example.com
roomctl upload ~/slides.pdf
roomctl home
roomctl -t spare status                           # a second display

roomctl screens                                   # what monitors this Pi has
roomctl navigate https://example.com -s right     # one monitor
roomctl navigate https://example.com -s all       # both
roomctl scroll --down | --up | --top | --bottom
roomctl autoscroll start -s right --speed 60

roomctl navigate https://youtube.com/watch?v=...  # then drive it:
roomctl media                                     # what is playing
roomctl media toggle                              # play / pause
roomctl media seek -30                            # seconds, negative rewinds
roomctl media volume 40                           # 0-100
roomctl media mute

roomctl window minimized                          # kiosk aside, Pi's desktop free
roomctl window fullscreen                         # and back on its own monitor

roomctl inspect                                   # title, error_page, form fields
roomctl click "#login"                            # needs [interact] enabled
roomctl type "kramav"
roomctl key ctrl+a

roomctl shot -o wall.png                          # what is ACTUALLY on the wall
roomctl shot                                      # just the size, url and title
roomctl shot -s right --region 0,0,800,600        # part of one monitor
roomctl shot --format jpeg --quality 50 -o q.jpg  # smaller over a slow link

roomctl extension list                            # ad blockers etc.
roomctl extension install <store-id> [<id>...]    # live at the next browser start
roomctl extension remove <store-id>
```

A **target** is a Pi; a **screen** is one monitor on it. Omit `-s` and you get
the first screen, which is the whole story on a single-monitor display.

Every command prints the agent's JSON reply, so it pipes into `jq`; errors go to
stderr with exit 1. `targets.toml` holds bearer tokens and is git-ignored.
`ROOMCTL_TARGETS` overrides its location. No install needed on a box that just
has the checkout: `python -m roomctl status`.

**From another program** — a script, a scheduler, eve. Use `roomctl.Client`: it
takes the url and token directly, so nothing has to write a `targets.toml` to
disk, and it holds one connection open instead of dialling per call.

```python
import os, roomctl

with roomctl.Client(os.environ["CROSSDROP_URL"], os.environ["CROSSDROP_TOKEN"]) as c:
    s = c.status()
    c.navigate("https://example.com", screen="all")

    if "autoscroll" in s["supports"]:          # ask, don't collect 501s
        c.autoscroll("start", speed=60)

    staged = c.upload("slides.pdf", navigate=False)   # prepare, show later
    c.navigate(os.environ["CROSSDROP_URL"] + staged["url"])
```

Failures are typed. `AgentError` subclasses `RuntimeError` — so anything written
against the old client still works — and carries `.status` and `.detail`:

```python
try:
    c.scroll()
except roomctl.Unreachable:   # the box is off, or the tailnet is down
    ...
except roomctl.Unsupported:   # 501: this browser can't. Check status()["supports"]
    ...
except roomctl.Unavailable:   # 503: agent is up, its browser isn't
    ...
except roomctl.NotFound:      # 404: no such screen or file, nothing playing
    ...
```

`roomctl.client("study")` builds one from a named target if you do want
`targets.toml`:

```python
with roomctl.client("study") as c:      # url + token out of targets.toml
    c.home(screen="all")
```

> **Breaking, v1.3.0:** the by-name module functions (`roomctl.status(target)`,
> `roomctl.navigate(url, target, screen)`, …) are gone. Each was one line of
> `with client(target) as c: return c.method(...)` with the arguments in a
> different order, and every new endpoint had to be written in three places.
> Replace `roomctl.navigate(url, "study")` with
> `with roomctl.client("study") as c: c.navigate(url)`. `roomctl.Client` and
> `roomctl.client` are unchanged, and the `roomctl` CLI is unaffected.

## Video and audio

Put a video on the wall the usual way — a URL, or a dropped file — and the
controller grows transport controls for it: play/pause, ±10 s, mute, volume,
and the position. The web UI shows them only while the screen actually has a
`<video>` or `<audio>` on it, so they appear and disappear on their own.
<kbd>Space</kbd>, <kbd>←</kbd> and <kbd>→</kbd> work there too. The tray's
right-click menu has **Play/pause**; the CLI has `roomctl media`.

Everything acts on the media element itself, which means:

- **It needs Chromium or Edge.** Firefox has no CDP and returns `501`, exactly
  as scroll does.
- **A player inside a cross-origin `<iframe>` is invisible to it** — a YouTube
  *watch page* is fine, a site embedding a YouTube player is not.
- **Volume is the page's, not the Pi's.** If HDMI audio is muted in
  `alsamixer` on the Pi, nothing here will make a sound; set that once at
  install.
- **Uploads are RAM** (the store is tmpfs), so a long film belongs at a URL.
  Raising `upload.max_mb` raises what a single drop costs the Pi.
- A film longer than `display.content_off_minutes` (default 2 h) still blanks
  the screen mid-playback; raise it in `config.toml` for a cinema room.

## Seeing what is on the wall

Every other route in this API reports the url it was *given*. `POST /v1/navigate`
returns what you sent, so a redirect, an expired login, a consent banner and a
crashed tab all look identical to success. `POST /v1/screenshot` is the
read-back:

In the web UI that is the **Look** button: one press, one picture, shown under
the controls with a *save* link. With **All** armed it fans out client-side and
shows every monitor side by side. Send anything to the display afterwards and
the pictures dim — they are of the page *before* that, and a stale screenshot
presented as current is worse than none.

From a terminal:

```sh
roomctl shot -o wall.png && start wall.png     # Windows; `xdg-open` on Linux
```

```python
import base64, pathlib, roomctl
with roomctl.Client(url, token) as c:
    shot = c.screenshot(screen="left")
    pathlib.Path("wall.png").write_bytes(base64.b64decode(shot["image"]))
    print(shot["title"], shot["width"], "x", shot["height"])
```

**It captures the page, not the screen.** This renders the frame tree of one
browser window over the same CDP connection `navigate` uses, so it can no more
see the Pi's desktop, its other windows or its taskbar than `navigate` can drive
them. That is the deliberate boundary: enough to verify and diagnose what the
display is showing, and structurally incapable of being a remote desktop. There
is no streaming and no push — a screenshot happens because someone asked for
one.

Worth knowing:

- **Image pixels are CSS pixels.** The clip is always sent at `scale: 1`, so a
  1920-wide viewport is a 1920-wide image even on a HiDPI panel, where an
  unpinned capture would come back 3840 wide.
- **`region` is clamped, not rejected** — `{"x": 100, "y": 80}` means "everything
  below and right of there". The returned `width`/`height` are what you got.
- **It does not wake the display.** Looking at a screen is not "show me
  something", so a poller cannot light the room all night. The page is rendered
  whether or not the monitor is powered.
- **The image is base64 in the JSON**, so it drops straight into a
  `data:image/png;base64,…` url. `--format jpeg` bounds the *worst* case — a
  photo or a video frame runs to megabytes as PNG — but do not assume it is
  always smaller. On a flat page PNG usually wins outright, because it
  compresses a white background almost perfectly while JPEG still pays for a
  colour profile and its blocks. Measured on the smoke-test fixture: 21 KB PNG
  against 33 KB JPEG.
- **Chromium or Edge.** Firefox 501s, as with scroll and media — though unlike
  those, BiDi does have the primitive, so it is unwritten rather than impossible.
- **`title` is whatever the browser calls the page right now.** For a moment
  after a navigate that is Chromium's provisional title — the bare host — because
  `Page.navigate` returns on commit, before the `<title>` is parsed.
  `GET /v1/inspect` reads `document.title` and is the one to ask if you need the
  real answer the instant you land.
- **Never route a screenshot through `/files/{id}`.** That path is
  unauthenticated by design; putting rendered page content behind it would
  publish whatever the kiosk is logged into.

`GET /v1/inspect` is the same question answered for a program, which cannot look
at a picture: title, ready state, scroll position, and `error_page` — which
catches Chromium's own crash and network pages, the ones that render perfectly
and answer `/v1/status` with a cheerful 200.

```sh
roomctl inspect | jq '{title, error_page, fields: [.fields[].selector]}'
```

It never reports a field's **value**. Naming a password box is how you know
where to type; handing back what is in it would make a diagnostic a leak.

## Typing on the wall

The Pi has no keyboard. When a school login expires or a consent wall appears,
the display is stuck on a page nobody can get past — and that is the one failure
this whole project cannot otherwise recover from.

`POST /v1/input` is the way out, and it **ships off**:

```toml
# /etc/crossdrop/config.toml — picked up within seconds, no restart
[interact]
enabled = true
```

Off, `input` is absent from `supports` and the route 501s, so clients hide it
rather than discovering it by failing. It is the only route here that acts *as*
whoever the kiosk is logged in as — everything else shows something or reads
something back — which is why it is the one thing behind a switch, and why that
switch lives in the root-owned `config.toml` the agent cannot write.

In the web UI: press **Look**, then **click the picture where you want to
click the page**. The capture is pinned to CSS pixels, so those are the same
coordinate space. Type into the box, tick *hide* for a password, press ⏎ to
submit. Every action is followed by a fresh capture, so you watch the form fill
in.

From a program, a whole login is one request:

```python
c.input([{"do": "click", "selector": "#user"},
         {"do": "type",  "text": user},
         {"do": "click", "selector": "#pass"},
         {"do": "type",  "text": password},
         {"do": "key",   "key": "Enter"}])
```

```sh
roomctl click "#user" && roomctl type "kramav" && roomctl key Enter
roomctl click 812 442 --right
```

Actions are `click`, `double`, `right`, `move`, `drag`, `type`, `key`, `wait`.
Prefer `selector` over `x, y` — it survives a reflow, and it scrolls the element
into view first.

Worth knowing:

- **It stops at the first failure.** Half a login is the dangerous half: if the
  click that focuses the password box missed, the next action would type the
  password into whatever does have focus. `results` names the action that
  stopped it, and `ok` is `false` — a 200, because some of it did happen.
- **A malformed list runs none of it.** Structure is checked before anything is
  dispatched, because there is no undo.
- **Input goes into the kiosk page, nowhere else.** Same CDP target as
  `navigate` — it cannot alt-tab, reach the window manager, close the kiosk, or
  type into any other application on the box.
- **The log records that you typed, never what.** `type(7 chars)`, not the
  password.
- **One request, one deadline.** Default 30 s, capped by `[interact]
  deadline_ms`; a caller may ask for less, never more.

## Upgrading from v1.x

**v2.0.0 renamed the installation.** v1.x lived at `/opt/room-display` with a
unit called `display-agent`; v2 lives at `/opt/crossdrop` with `crossdrop-agent`,
and the `ROOM_*` environment variables are now `CROSSDROP_*`. One name instead
of three — `systemctl --user status crossdrop-agent` is now the obvious guess
*and* the right one.

On an existing Pi, run the migration once:

```sh
# on the box, over SSH, as the user that owns the graphical session.
# migrate.sh is new in v2, so it is not on a v1 box yet — fetch it:
curl -fsSL https://raw.githubusercontent.com/Kramav/CrossDrop/main/deploy/pi/migrate.sh | bash
```

Re-running the installer on a v1 box refuses and prints that same line, so you
cannot get this wrong by accident. `MIGRATE=1` on the installer does both in one
step.

It stops the update timer first, moves `/etc/room-display` and the profile
snapshot (your browser logins), backs the config up beside itself, then hands
off to `setup.sh` to rebuild the code. **The bearer token is not regenerated**,
so every controller's `targets.toml` keeps working. Nothing irreplaceable is
deleted — only the git checkout, which is rebuilt from the repo.

If you would rather start clean: `bash .../uninstall.sh` then re-run the
installer. The uninstaller understands both layouts, so it works on a box that
was never migrated.

You do not have to do this today. Until you migrate, the Pi keeps running the
release it has: `agent/selfcheck.py` refuses the v2 layout on a v1 box and
`update.sh` gates the release swap on selfcheck, so an unmigrated Pi that sees
the v2 tag **declines it and stays healthy** rather than swapping onto code
whose paths do not exist. The reason appears in `journalctl --user -u
display-agent`.

## The `/v1` contract

Frozen. Bearer token on every route; FastAPI publishes the schema at `/docs`.

Every route takes an optional `screen` (a name, or `"all"`). Omitted means the
first screen — multi-monitor was added by *adding* a field, so a client written
before it keeps working unchanged.

| Route | Body | Returns |
|---|---|---|
| `POST /v1/navigate` | `{"url": "...", "screen"?}` | `{"ok": true, "current_url": "...", "screens": [...]}` |
| `POST /v1/upload` | multipart `file`, `screen`?, `navigate`? | `{"id": "...", "url": "/files/<id>"}`, then auto-navigates unless `navigate=false` |
| `POST /v1/reload` | `{"screen"?}` | as `navigate` |
| `POST /v1/home` | `{"screen"?}` | as `navigate` |
| `POST /v1/scroll` | `{"screen"?, "dy"?, "to"?}` | `to` is `"top"`\|`"bottom"`; else `dy` pixels |
| `POST /v1/autoscroll` | `{"screen"?, "action", "speed"?}` | `action` is `"start"`\|`"stop"` |
| `POST /v1/media` | `{"screen"?, "action", "value"?}` | `{"ok", "playing", "muted", "volume", "position", "duration"}`; 404 when nothing is playing |
| `POST /v1/window` | `{"screen"?, "state"}` | `"normal"`\|`"minimized"`\|`"fullscreen"` — the way out of `--kiosk` without stopping the agent |
| `POST /v1/screenshot` | `{"screen"?, "region"?, "format"?, "quality"?}` | `{"image"` (base64)`, "width", "height", "url", "title", …}`. No `"all"` — one request, one picture |
| `GET /v1/inspect` | `?screen=` | `{"url", "title", "ready_state", "error_page", "scroll_y", "fields": [...]}`. No field *values* |
| `POST /v1/input` | `{"screen"?, "actions": [...], "deadline_ms"?}` | `{"ok", "results": [...]}`. **501 unless `[interact] enabled = true`** |
| `GET /v1/extensions` | — | `{"installed": [{"id", "name"}], "pending_restart"}` |
| `POST /v1/extensions` | `{"ids": ["…"]}` | Web Store ids, never urls. Installs unpacked; live at the next browser start |
| `DELETE /v1/extensions/{id}` | — | as `GET` |
| `GET /v1/screens` | — | `[{"name", "position", "current_url", "autoscroll"}]` |
| `GET /v1/settings` | — | editable screen settings + what `xrandr` detects now |
| `PUT /v1/settings` | `{"screens": [{"name", "home_url"}], "splits"?, "mode"?}` | saves, re-reads the layout from `xrandr`, then moves the windows live |
| `POST /v1/relaunch` | — | restarts the kiosk browser; the only way to apply `mode` or a split |
| `GET /v1/status` | — | `"screens"`, plus `"kind"`, `"supports"`, `"started_at"`, `"error"` |

Three things to know if the caller is a program rather than a person:

- **Check `supports` before you call.** `/v1/status` reports the browser `kind`
  and the list of things it can do. Chromium and Edge do everything; Firefox
  does `navigate` and nothing else — `scroll`, `autoscroll`, `media` and any
  screen but the first all return **501**. The dev default is Firefox and the Pi
  runs Chromium, so this genuinely differs between boxes.
- **`screen: "all"` reports per screen.** The fan-out is not atomic, so
  `screens` carries `{"name", "ok", "current_url", "error"}` for each one and
  top-level `ok` is `false` if any of them failed. Every screen failing is still
  a 503; one *named* screen failing is still a 503. `current_url` is the last
  screen that worked, unchanged.
- **`current_url` in a navigate reply is what we sent, not what loaded.** A
  redirect or a login wall still reports the URL you asked for. `GET /v1/screens`
  is the read-back that tells you what is actually up — and
  `POST /v1/screenshot` is the one that shows you.

`started_at` changes when the agent restarts. That matters because the nightly
restart timer drops any running autoscroll, and a poller has no other way to
notice.

### Two frozen warts

Both are wrong, both stay, because `/v1` is frozen and a client that reads them
would change behaviour the day they were fixed.

- **`up` is not a health check.** It is always `true` and means "this agent
  answered", which the HTTP 200 already told you. **`browser` and `error` are
  the fields that carry news** — check those.
- **`/v1/autoscroll` puts a screen *name* in `current_url`.** It reuses the
  `navigate` reply shape but fills it with the last targeted screen's name, and
  leaves `screens` empty — unlike every other fan-out route. Read the status
  code; `GET /v1/screens` carries the real per-screen `autoscroll` flag.

**The agent outlives a browser that will not start.** No binary, a debug port
that never comes up, an X session slower than the agent — none of them stop it
serving. `browser` reads `"down"` as it always has, and `"error"` says why:

```sh
roomctl status | jq -r '.browser, .error'
# down
# RuntimeError: no chromium binary found; set browser.path in config
```

It keeps retrying in the background (5s, doubling to 5 min), so the genuinely
transient case — the compositor was not up yet — clears itself with no restart
and `error` goes back to `""`. Empty means nothing to report, so a client can
treat any non-empty value as a real problem. This is the one field you want on
a box with no keyboard: the alternative was the agent exiting, systemd
restarting it into the same failure, and no `/v1/status` alive to be asked.

**Every request is logged**, to journald via the service's stderr:

```sh
journalctl --user -u crossdrop-agent -f                  # follow
journalctl --user -u crossdrop-agent | grep /v1/navigate # what was put on the wall
```

Mutations log at INFO, reads at DEBUG — a controller polling `/v1/status` every
15s would otherwise bury the one navigate you are looking for, and the Pi's
journal is 32 MB and in RAM. Set `CROSSDROP_LOG=DEBUG` in the unit to see the reads
too.

`GET /home` is the idle screen the kiosk sits on, and `GET /home-status` feeds
it. Both are unauthenticated for the same reason `/files` is — the kiosk browser
can't send a header. `/home-status` reports the **host** of what other screens
are showing, never the full URL.

`GET /files/{id}` is unauthenticated on purpose — the kiosk browser fetches it
and cannot send a header. The random id is the capability, and ids are never
listed.

## Tests

From the repo root, with `agent/requirements.txt` installed:

```sh
pytest                  # no browser needed
CROSSDROP_SMOKE=1 pytest -s  # drives a real kiosk browser
```

The smoke tests are where the claims a stub cannot check get checked — that a
capture really is in CSS pixels, that `error_page` is true on a real Chromium
error page, and that a click at given coordinates really lands on the element
that is there. They open one kiosk window for about half a minute:

```sh
CROSSDROP_BROWSER=chromium CROSSDROP_SMOKE=1 pytest tests/test_smoke.py -q
```

On the Pi they need the agent stopped first — it holds the debug port, and the
tests refuse to start rather than silently drive the live display.
[deploy/pi/smoke-on-the-pi.md](deploy/pi/smoke-on-the-pi.md) is the walkthrough.
