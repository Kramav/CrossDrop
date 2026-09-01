# CrossDrop

Drive a keyboard-less Raspberry Pi room display from any box on the tailnet:
point it at a URL, or drop a file on it and have it render.

- **Pi setup** — [deploy/pi/pi-setup.md](deploy/pi/pi-setup.md) (OS) then
  [deploy/pi/README.md](deploy/pi/README.md) (agent). One script does both:
  [deploy/pi/setup.sh](deploy/pi/setup.sh).
- **Build plan and phases** — [PLAN.md](PLAN.md).

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

A **target** is a Pi. A **screen** is one monitor on it.

### Where things live

Each file's own docstring is the detailed version — the point of this table is
that you only have to open one.

| Path | What it is |
|---|---|
| [agent/app.py](agent/app.py) | The whole HTTP surface: routes, pydantic models, auth, config loading. The only file that defines the API. |
| [agent/browser.py](agent/browser.py) | Launches the kiosk browser, drives the tab. CDP *and* BiDi over one JSON-RPC helper. Navigate, scroll, media, window placement. |
| [agent/display.py](agent/display.py) | Monitor power only, via `xrandr`/DPMS. Idle watcher; every screen-touching route wakes the display first. X11 only. |
| [agent/storage.py](agent/storage.py) | Upload store. Extension allowlist, size cap, keep-newest-N sweep. It's tmpfs, so it's RAM. |
| [agent/settings.py](agent/settings.py) | The subset of config the agent may rewrite at runtime (screen names, home URLs, geometry) → `settings.json`. Everything else stays file-only in `config.toml`. |
| [agent/selfcheck.py](agent/selfcheck.py) | `python -m agent selfcheck` — in-process boot check that gates a release swap. |
| [agent/config.example.toml](agent/config.example.toml) | Install-time config: token, browser kind, ports, paths. Real one is git-ignored. |
| [roomctl/__init__.py](roomctl/__init__.py) | The client library — one function per route. `eve` imports this. |
| [roomctl/cli.py](roomctl/cli.py) | argparse shell over the above; prints the agent's JSON verbatim. |
| [web/index.html](web/index.html) | The controller UI the agent serves at `/`. Single file, no build step, no framework. |
| [web/home.html](web/home.html) | The idle screen the kiosk sits on. Also single-file. |
| [deploy/pi/](deploy/pi/) | Provisioning (`setup.sh`), systemd units, tmpfs profile snapshots, and `update.sh` — the release-gated auto-updater with rollback. |
| [deploy/windows/roomtray.ps1](deploy/windows/roomtray.ps1) | The tray client. Pure PowerShell + WinForms so it runs on a box with no checkout and no Python. |
| [tests/](tests/) | pytest, one file per surface. No browser needed unless `ROOM_SMOKE=1`. |
| [PLAN.md](PLAN.md) | Why it's built this way, phase by phase. Section numbers referenced from code comments. |
| [HANDOFF.md](HANDOFF.md) | Session-to-session state. Point-in-time; trust the code over it. |

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

**Web UI** — the drop zone the agent serves, and the only place with scroll
controls and saved links. Open it as its own window:

```powershell
msedge.exe --app=http://<pi-tailnet-ip>:8080/
```

Paste a link or drag a file (`.pdf .png .jpg .jpeg .gif .webp .txt`, plus
`.mp4 .webm .mp3 .m4a .wav`, 25 MB cap)
and the display follows. Paste the agent token into **Settings** on first run;
it stays in that browser.

**Settings** also holds the screens editor — each monitor's name, home URL,
position and size, applied live with no restart, so moving a window between
monitors happens while you watch. Leave position or size blank and the agent
falls back to what `xrandr` detects; **Re-detect layout** is that, for both.

Edits persist to `~/.local/share/room-display/settings.json`, which the agent
owns and `update.sh` never touches. They do **not** go into
`/etc/room-display/config.toml` — it holds the bearer token and is deliberately
`root:<user> 640`, so the agent cannot write it. The token, `browser.kind`,
`profile_dir`, `upload.dir` and `debug_port` stay file-only for the same reason:
they are install-time facts that need a browser relaunch, not a config reload.

**`roomctl`** — the CLI, and the same functions eve imports.

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
```

A **target** is a Pi; a **screen** is one monitor on it. Omit `-s` and you get
the first screen, which is the whole story on a single-monitor display.

Every command prints the agent's JSON reply, so it pipes into `jq`; errors go to
stderr with exit 1. `targets.toml` holds bearer tokens and is git-ignored.
`ROOMCTL_TARGETS` overrides its location. No install needed on a box that just
has the checkout: `python -m roomctl status`.

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

## The `/v1` contract

Frozen. Bearer token on every route; FastAPI publishes the schema at `/docs`.

Every route takes an optional `screen` (a name, or `"all"`). Omitted means the
first screen — multi-monitor was added by *adding* a field, so a client written
before it keeps working unchanged.

| Route | Body | Returns |
|---|---|---|
| `POST /v1/navigate` | `{"url": "...", "screen"?}` | `{"ok": true, "current_url": "..."}` |
| `POST /v1/upload` | multipart `file`, `screen`? | `{"id": "...", "url": "/files/<id>"}`, then auto-navigates |
| `POST /v1/reload` | `{"screen"?}` | `{"ok": true, "current_url": "..."}` |
| `POST /v1/home` | `{"screen"?}` | `{"ok": true, "current_url": "..."}` |
| `POST /v1/scroll` | `{"screen"?, "dy"?, "to"?}` | `to` is `"top"`\|`"bottom"`; else `dy` pixels |
| `POST /v1/autoscroll` | `{"screen"?, "action", "speed"?}` | `action` is `"start"`\|`"stop"` |
| `POST /v1/media` | `{"screen"?, "action", "value"?}` | `{"ok", "playing", "muted", "volume", "position", "duration"}`; 404 when nothing is playing |
| `GET /v1/screens` | — | `[{"name", "position", "current_url", "autoscroll"}]` |
| `GET /v1/settings` | — | editable screen settings + what `xrandr` detects now |
| `PUT /v1/settings` | `{"screens": [{"name", "home_url", "position"?, "size"?}]}` | saves, then moves the windows live |
| `GET /v1/status` | — | as before, plus `"screens": [...]` |

`GET /home` is the idle screen the kiosk sits on, and `GET /home-status` feeds
it. Both are unauthenticated for the same reason `/files` is — the kiosk browser
can't send a header. `/home-status` reports the **host** of what other screens
are showing, never the full URL.

`GET /files/{id}` is unauthenticated on purpose — the kiosk browser fetches it
and cannot send a header. The random id is the capability, and ids are never
listed.

## Tests

```sh
pytest                  # no browser needed
ROOM_SMOKE=1 pytest -s  # drives a real kiosk browser
```
