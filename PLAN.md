# Room Display Control — Build Plan (v3)

**Goal:** Push reference material (URLs and dropped files) to one Raspberry Pi display, from a Windows 10 desktop and a Windows 11 laptop (and later `eve`). The Pi runs a desktop session with a kiosk browser; the browser uses an ephemeral RAM profile with only login/auth state persisted. The Pi auto-updates itself from GitHub on release tags.

**Core principle:** The Pi agent exposes **one versioned HTTP API (`/v1/...`)**. Every control surface — web UI, CLI, `eve`, a future downloadable native app — is just a **client** of that API. The server never changes when a client is added.

> **Status:** *Inferred architecture* (no single upstream guide). Each phase names the authoritative docs to pull; verified links are provided **before** that phase's code is written. Generated code is kept distinct from doc-based steps (systemd, browser flags, OpenAPI, GitHub Actions).
>
> **v3 changes:** added Phase 8 (release-gated auto-update / CI-CD); added desktop auto-login to Phase 5; added `version` to `/v1/status`.

---

## 1. Scope & assumptions

**Locked (from you):**
- Transport: **HTTP**, versioned API (`/v1`).
- Persistence: **RAM-first** browser profile; persist only login/auth state.
- **One target:** the study Pi (server + display). It has a display + attached screens, **no keyboard/mouse**.
- **Two controllers:** Win10 desktop + Win11 laptop (stateless clients).
- Control surface **now**: web UI opened as its own app window. **Future**: downloadable native app + CLI, both clients of the same API. `eve` later.
- Auto-update: Pi pulls and deploys **only on release tags you cut** (release-gated).

**Assumptions (correct if wrong):**
- A1. Material is URLs **and** dropped local files (PDF/image).
- A2. Pi boots from **SD** (drives the RAM-profile design). USB-SSD → Phase 6 tmpfs work becomes optional.
- A3. Cross-machine reach via **Tailscale**.
- A4. Pi runs **64-bit** Pi OS (clean FastAPI/pydantic install).
- A5. Pi runs Raspberry Pi OS **with the desktop**, with **auto-login to the desktop session** (the kiosk needs a graphical session at boot with no human present).
- A6. The GitHub repo is **private** (deploy-key auth; keeps auto-deploy safe).

---

## 2. Architecture

```
 CONTROLLERS (clients)                  PI (server + display)
 ┌──────────────────────┐              ┌─────────────────────────────────┐
 │ Win10 desktop        │              │  crossdrop-agent (FastAPI)         │
 │  web app-window ─────┼──HTTP /v1──▶ │   GET /            (web UI)      │
 │  roomctl CLI ────────┼──HTTP /v1──▶ │   POST /v1/navigate  ──CDP──▶    │
 ├──────────────────────┤   (Tailscale)│   POST /v1/upload -> tmpfs      │  Chromium
 │ Win11 laptop         │              │   GET  /files/{id}              │  kiosk
 │  web app-window ─────┼──HTTP /v1──▶ │   GET  /v1/status               │  (desktop
 ├──────────────────────┤              │   RAM profile + cookie snapshot │   session)
 │ eve (future client) ─┼──HTTP /v1──▶ └─────────────────────────────────┘
 └──────────────────────┘                        ▲
   future: native C# app = another /v1 client    │ pull on new release tag
                                          ┌───────┴────────┐
                                          │ GitHub (private)│
                                          └────────────────┘
```

- **Server:** FastAPI on the Pi, running inside the logged-in desktop session. Serves web UI at `GET /`, JSON API under `/v1`, uploads at `/files/{id}`. Drives the local kiosk browser via CDP `Page.navigate`.
- **Clients:** web UI (same-origin `fetch`), `roomctl` CLI, `eve`, future native app.
- If the Pi is down, control is down — acceptable, it's the only target.

---

## 3. Tech stack (pinned) + rationale

| Component | Choice | Why | Alt |
|---|---|---|---|
| Agent / API | **FastAPI + uvicorn** (Py 3.11+) | Auto OpenAPI + `/docs`; future C# app codegens a typed client | Flask (only if you forgo the schema) |
| Browser control | **Raw CDP** (`websocket-client` + stdlib HTTP to `/json`) | Minimal deps on the Pi, no bundled-browser download | pychrome; Playwright (if page interaction ever needed) |
| Browser (Pi) | **Chromium** | Preinstalled | — |
| Shared client | **`roomctl`** Python pkg | CLI + eve import the same functions | — |
| Web UI | **Static HTML/JS** (drop-zone + paste + saved buttons) | HTML5 drag-drop handles links *and* files; same-origin → no CORS | — |
| App-window feel | **Edge/Chrome `--app=<pi-url>`** | Chromeless own window over http (no PWA/https needed) | Install-as-PWA if you add https |
| Auto-update | **systemd timer + `update.sh`**, release-gated | Readable, debuggable, no inbound exposure; you control when | Self-hosted Actions runner (adds CI gating; never on a public repo); balena/Mender (fleet-scale OTA, overkill for 1 Pi) |

---

## 4. Repo layout

As built. Where this differs from the original sketch, the sketch was wrong:
there is no `kiosk-launch.sh` (the agent launches the browser itself), no
`cookie-*.sh` (one `profile-snapshot.sh` takes the whole profile minus caches),
no `web/app.js` (the page is one self-contained file), and CI lives at
`.github/workflows/` because that is where GitHub looks.

```
CrossDrop/
├── agent/
│   ├── app.py                # FastAPI: every /v1 route, GET /, /home, /files
│   ├── browser.py            # launch the kiosk; CDP (chromium/edge) or BiDi (firefox)
│   ├── display.py            # DPMS power + idle blanking via xset/xrandr
│   ├── settings.py           # the runtime-editable subset, in settings.json
│   ├── storage.py            # tmpfs upload store (size/type caps, id map)
│   ├── selfcheck.py          # boot sanity for update.sh
│   ├── __main__.py           # `python -m agent selfcheck | serve`
│   ├── config.example.toml
│   └── requirements.txt      # fastapi, uvicorn, websocket-client, python-multipart
├── roomctl/                  # client lib (the CLI and eve import this)
│   ├── __init__.py           # Client(url, token) + typed errors + by-name functions
│   ├── cli.py                # `roomctl navigate <url>`
│   └── targets.example.toml
├── web/
│   ├── index.html            # drop-zone, paste, saved refs, screens editor
│   └── home.html             # the idle screen the kiosk sits on
├── deploy/
│   ├── pi/
│   │   ├── setup.sh                    # one re-runnable installer, Pi and plain Debian
│   │   ├── update.sh                   # release-gated pull + health-check + rollback
│   │   ├── profile-snapshot.sh         # profile minus caches, so auth survives a reboot
│   │   ├── crossdrop-agent.service
│   │   ├── crossdrop-update.{service,timer}    # ~30 min, ships disabled
│   │   ├── crossdrop-snapshot.{service,timer}  # hourly, ships disabled
│   │   ├── crossdrop-restart.{service,timer}   # 04:00 ±15m, ships ENABLED
│   │   ├── journald-volatile.conf
│   │   ├── pi-setup.md
│   │   └── README.md
│   ├── windows/              # tray app: roomtray.ps1, selfcheck.ps1, README
│   └── linux.md
├── .github/workflows/ci.yml  # lint + tests on push/PR to main
├── tests/                    # pytest; no browser needed unless CROSSDROP_SMOKE=1
└── README.md                 # documents the frozen /v1 contract
```

---

## 5. The `/v1` contract (freeze early)

- `POST /v1/navigate` `{ "url": "..." }` → `{ "ok": true, "current_url": "..." }`
- `POST /v1/upload` (multipart file) → `{ "id": "...", "url": "/files/<id>" }`, then auto-navigate
- `GET  /v1/status` → `{ "up": true, "current_url": "...", "browser": "ok", "version": "<tag>" }`
- `POST /v1/reload`, `POST /v1/home`
- `POST /v1/display` `{ "action": "on"|"off" }` → `{ "ok": true, "awake": bool }`.
  No `screen`: X11 powers every monitor together. Every other route wakes the
  display as a side effect, so this exists for turning it **off** on the way out.
- `POST /v1/media` `{ "screen"?, "action", "value"? }` → `{ "ok", "playing", "muted", "volume", "position", "duration" }`.
  `action` is `state|play|pause|toggle|mute|unmute|seek|volume`; `value` is
  seconds for `seek` (negative rewinds) and 0-100 for `volume`. `404` when the
  page has no `<video>`/`<audio>`, `501` on Firefox. Additive — no existing
  route or field changed.
- Auth: `Authorization: Bearer <token>` on all `/v1` routes.

Published by FastAPI at `/docs` + `/openapi.json`. **v1 semantics frozen** once the native app targets it.

---

## 6. Known gotchas / correctness flags
- **CDP origin check** — Chrome ≥ 111 rejects CDP websockets carrying an `Origin` header. Do **not** fix this with `--remote-allow-origins=*`: the check exists to stop the arbitrary page we render from reaching `:9222`, and the whole shared cookie jar is behind that port. `browser._rpc` sends no `Origin` at all (`suppress_origin=True`), which satisfies the check without disabling it.
- **Kiosk needs a desktop session** — the agent launches the browser into the logged-in graphical session (its `DISPLAY`/Wayland socket); the service runs **as that user**, not as a detached system daemon.
- **Keep `:9222` on 127.0.0.1** — debug port = full browser control. Only the FastAPI port is exposed (tailnet).
- **SSO expiry** — persisted cookies reduce, not eliminate, school re-logins.
- **Unclean shutdown (Pi)** — stop-only tmpfs snapshot loses the last session on power-loss (Phase 6 options).
- **Upload safety** — cap size (tmpfs = RAM), allowlist types, sanitize filename, dedicated dir.
- **32-bit Pi OS** — pydantic-core may need compiling; 64-bit ships aarch64 wheels. Confirm A4.
- **`--user-data-dir` must be dedicated/free** or the kiosk launch is ignored.
- **Auto-update on a no-input box** — a bad update can't be fixed with a local keyboard, so health-check + rollback in Phase 8 are mandatory, not optional.

---

## 7. Phased build (each phase ends with an acceptance test)

**Phase 0 — Scaffold.** Repo per §4, requirements, `.example` configs, `.gitignore` (real configs, snapshots, uploads, releases). *Accept:* `pip install -r agent/requirements.txt` on dev machine.

**Phase 1 — Agent core + frozen `/v1` contract (dev machine).** FastAPI; `browser.py` launches kiosk with `--remote-debugging-port=9222 --remote-allow-origins=* --kiosk --user-data-dir=<dedicated>`; `POST /v1/navigate` via raw CDP; `GET /v1/status`; bearer auth; URL-scheme allowlist. *Accept:* `curl` navigates the local tab; `/docs` shows the schema.

**Phase 2 — `roomctl` client + CLI (both controllers).** Shared lib + `roomctl navigate <url>`; `targets.toml` → the one Pi. *Accept:* from desktop **and** laptop, `roomctl navigate https://example.com` drives the Pi over Tailscale.

**Phase 3 — Web UI.** `GET /` serves drop-zone + paste + saved buttons; `app.js` calls `/v1/navigate`; open via `--app=<pi-url>`. *Accept:* paste or drop a **link** in the app window → Pi navigates.

**Phase 4 — File drop.** `POST /v1/upload` → tmpfs store (caps/validation) → serve at `/files/{id}` → auto-navigate. *Accept:* drop a **PDF** → it renders on the Pi.

**Phase 5 — Pi provisioning (persistent profile first).**
- Enable **desktop auto-login** (`raspi-config` → System Options → Boot / Auto Login → **Desktop Autologin**) so the graphical session + browser come up after reboot with no human present.
- `crossdrop-agent.service` runs **as the login user**, tied to `graphical-session.target`; `kiosk-launch.sh`; on-SD profile to isolate variables.
- *Accept:* reboot Pi → desktop auto-logs in → kiosk + agent up → control works with nobody present.

**Phase 6 — Pi RAM profile + auth persistence.** tmpfs profile; restore-on-boot, snapshot-on-stop (± hourly timer) of profile **minus** `Cache/`,`Code Cache/`,`GPUCache/` (whole-minus-cache, so Local Storage / IndexedDB auth survives too). Add **log2ram** for `/var/log`. *Accept:* log into a school page → reboot → still authed (or graceful re-auth); idle SD writes ≈ 0.

**Phase 7 — eve integration.** eve imports `roomctl`; map intents → `navigate("study", url)`. *Accept:* a voice command changes the Pi screen.

> **Phase 7a — external control, done.** "It's an HTTP API, import the client" turned out not to be the whole answer. The API was built for a person driving a display; a program additionally has to know what it can do, learn whether what it did worked, and tell failure modes apart. None of that was available. Fixed, all additive to the frozen `/v1`:
>
> - `roomctl.Client(url, token)` — the library could only be *configured*, never *constructed*: every entry point went through `targets.toml` on disk. A caller holding a url and a token had to write TOML to use it. Also gets connection reuse.
> - Typed errors — `AgentError(RuntimeError)` with `.status`/`.detail`, plus `Unreachable`/`NotFound`/`Unsupported`/`Unavailable`. Previously every failure was one `RuntimeError` whose only machine-readable part, the status code, had been formatted into an English sentence.
> - `/v1/status` reports `kind`, `supports` and `started_at`. Capability discovery was by provoking 501s — and since the dev box ships Firefox and the Pi ships Chromium, the two expose genuinely different APIs. `started_at` is how a poller notices the 04:00 restart threw its autoscroll away.
> - **Firefox no longer lies about screens.** `browser.navigate`/`current_url` ignored the `screen` argument on the BiDi path, so `screen: "all"` on two monitors drove the first one twice and reported success both times. Now 501, like `scroll` and `media` already did.
> - `NavigateOut.screens[]` — the fan-out is not atomic and reported only the last screen's url, so a caller could not learn which monitors took a request. Per-screen `{name, ok, current_url, error}`; `ok: false` for partial. All screens failing is still a 503, and one *named* screen is still a 503.
> - `upload navigate=false` stages a file without putting it on the wall; `[server] host/port` + `python -m agent serve` means something other than systemd can start an agent.
>
> Still true and deliberately unfixed: `current_url` in a navigate reply is the url we *sent* (`GET /v1/screens` is the read-back); `/v1/media` raises on the first bad screen rather than collecting; no push, no logging, no per-caller tokens, no ETag on settings. See `tests/test_control.py`.

**Phase 8 — Auto-update from GitHub (release-gated CD).** See §8 below for the full design. *Accept:* tag a deliberately broken commit → Pi's health-check fails → it stays on the previous good version (logged); tag a good commit → Pi updates within one timer interval and `/v1/status.version` shows the new tag.

**v1.1.0 — screens editor in the web UI.** Built. Edit a screen's `position`, `size`, `home_url` and name from the drop-zone page instead of `ssh` + `nano` + restart, applied **live** via `browser.place()`. Persists to `~/.local/share/crossdrop/settings.json` — **JSON, not the planned `screens.toml`**, because `tomllib` only reads and a TOML writer is a new dependency for a file no human edits. `/etc/crossdrop/config.toml` stays un-writable by the agent as specified, and `token`, `profile_dir`, `upload.dir`, `debug_port` and `browser.kind` stay file-only. Blank `position`/`size` falls back to `display.detect()`, which is the re-detect path the UI exposes as a button. Same work put the token in a real field on the page instead of a `prompt()`. See `agent/settings.py`, `tests/test_settings.py`. *Accept (still unrun on hardware):* move a window between monitors from the UI, with no restart, and have it survive one.

**Deliberately still out of scope after v1.1.0.** Display sleep timeouts (`idle_off_minutes`, `content_off_minutes`) and upload caps (`max_mb`, `keep`) are runtime-safe and would drop into the same file in ~10 lines each; they were considered and left file-only. Add them when editing a config file over `ssh` is actually what stands in the way.

**v1.0.1 — agent-owned display power.** Done. The Pi has no keyboard, so anything that blanks the screen and wakes only on *input* can only be cured by unplugging the box. The agent claims DPMS at startup (timeouts zeroed, DPMS kept enabled) and drives power itself: idle on its home page → off after 10 min, showing a site → off after 2 h without a request, and **any `/v1` call wakes it**. `POST /v1/display` and a UI button cover leaving the room. Both monitors sleep together — X11 has no per-output power. Same commit made screens self-detecting (`xrandr --listmonitors`), so a fresh install needs no `[[screen]]` blocks written by hand. See `agent/display.py`, `deploy/pi/README.md` §8.

**v1.1.2 — playback control.** Done. A room display that can show a video could not pause one: the box has no keyboard, so whatever was pushed at it played to the end or not at all. `POST /v1/media` drives the page's own `<video>`/`<audio>` through one `Runtime.evaluate` — play, pause, ±10 s, mute, volume — with CDP's `userGesture`, which is what gets past Chromium's autoplay block on a page nobody can click. The web UI reveals its transport bar only when the screen really has a media element, the tray gets Play/pause, and `roomctl media` is the CLI. Uploads accept `.mp4 .webm .mp3 .m4a .wav` under the same tmpfs cap. Not covered: players inside cross-origin iframes (no execution context there) and the Pi's own ALSA volume. See `agent/browser.py` `media()`, `tests/test_media.py`.

**v1.1.7 — the agent survives its own browser.** Done. Three failures found by the 2026-09-06 architecture review, all of them curable only by walking into the room:

- **A failed browser launch took the API down with it.** `browser.launch()` ran inline in `lifespan`, so no binary, a debug port that never came up, or an X session slower than the agent raised *before* uvicorn bound the port. systemd restarted us, the next attempt failed identically, and `/v1/status` — the only thing that could have named the cause — was down for every attempt. Now it launches on the existing startup thread, retries with backoff (5s → 5 min, `CROSSDROP_LAUNCH_RETRY`), and reports the reason in a new `error` field on `/v1/status`. `browser` keeps its two original values, so a client reading `== "ok"` is unaffected. The retry is not a nicety: it is what replaces the systemd restart loop for the transient case, which was the one thing that loop got right.
- **An autoscroll restarted on the same screen orphaned the run that replaced it.** The finishing run popped whatever sat under its screen name, which after a second start was the *new* run's stop event. That run then scrolled with nothing holding its event: `POST /v1/autoscroll stop` popped nothing, the navigate guard in `_navigate_one()` stopped nothing, and the display went on scrolling every page sent to it afterwards — the exact haunting that guard exists to prevent. A lock, and a delete conditional on the entry still being ours. Trivially reachable by double-clicking the web UI's Auto-scroll button.
- **There was no log.** Six `print()` calls, none about a request. "The wall showed the wrong thing at 9am" was unanswerable with the journal in front of you. One middleware, one line per request: method, path, status, duration. Mutations at INFO and reads at DEBUG, because a 15s status poll at INFO buys thousands of lines a day against a 32 MB journal that lives in RAM. The level is set on our logger and not on root — root at INFO also turns on httpx, which narrates every one of `_home_when_ready`'s once-a-second polls.

`tests/test_resilience.py` is the surface: the agent surviving things, as against doing them. Each fix was confirmed to fail its tests when reverted. *Accept (unrun on hardware):* rename the Chromium binary, restart the agent, and read the reason out of `roomctl status` from a controller box rather than from a keyboard.

**v1.1.7 — screenshots, and two power/config fixes.** Done. Additive to the frozen `/v1`.

`POST /v1/screenshot` answers the thing this API could not: every other route reports the url it was *given*, so a redirect, an expired SSO login, a consent banner and a crashed tab are all indistinguishable from success. `roomctl shot -o wall.png`, `Client.screenshot()`, and `"screenshot"` in `supports` so a caller discovers it the same way it discovers everything else.

The design decisions worth keeping:

- **It captures the page, not the screen.** `Page.captureScreenshot` renders one browser target's frame tree over the CDP connection `navigate` already uses. It cannot see the Pi's desktop, its other windows or its taskbar. The remote-desktop boundary is therefore a property of the transport rather than a policy anyone has to enforce — and the two rules that keep it there are: everything goes through an existing CDP target, and screenshots stay request/response. No `Page.startScreencast`, which is the one way to cross the line without leaving CDP.
- **The clip is always sent at `scale: 1`.** Without an explicit clip Chromium captures at the device pixel ratio, so a 1920-wide viewport returns a 3840-wide image on a HiDPI panel and anything mapping the picture back onto the page is off by 2×. Pinned, image pixels *are* CSS pixels — the same space `Input.dispatchMouseEvent` takes, which is what makes this a foundation for interaction later rather than a dead end.
- **No `display.touch()`.** Looking at a screen is not "show me something"; waking the panel to photograph it would let a poller light the room all night. Same rule as `/v1/window` and `media action=state`.
- **No `"all"`.** One request, one picture. A list of images needs a second result model and nothing has asked for one.
- Deliberately unbuilt: streaming, desktop capture, OCR, template matching, and any screenshot path through `/files/{id}` — that route is unauthenticated by design and would publish whatever the kiosk is logged into.

Two fixes rode along, both from the same review:

- **`display.claim()` was fire-and-forget.** It runs while the agent is starting, which on a slow boot is before the session exists; a lost attempt was lost for good, leaving the session's own blanking timeouts to sleep the monitors with nothing able to wake them — the exact trap `display.py` exists to avoid. It now reports success and `watch()` retries until X takes it. The DPMS half is split from the power sync deliberately: re-claiming must not carry `power(True)` with it, or the tick after a deliberate `POST /v1/display off` would light the room back up.
- **The config swap could be read empty.** `clear()` then `update()` in `PUT /v1/settings` left `app.state.cfg` momentarily blank, and every browser route is `def` and runs on the threadpool, so a reader landing there got a `KeyError` and a 500. Now `swap_config()`: overwrite, then drop stale keys, so nothing present on both sides is ever absent. The window is a couple of bytecodes wide and a racing test passed just as happily with the bug in — so the test watches every mutation instead, which is deterministic.

The web UI got the **Look** button in the same work: one press, one picture, with an explicit *Clear*. `target === "all"` fans out **client-side** — the route stays one request, one picture, and a two-monitor wall still shows both. Anything sent to the display afterwards dims the pictures rather than removing them, because the comparison is usually what you wanted and a stale screenshot presented as current is worse than none. Captions are built with `createElement` + `textContent`: a page title comes off whatever arbitrary site is on the wall, and this page holds the agent token.

Three checks keep the boundary from eroding by accident, since none of it is enforced by anything a reader would notice:

- `tests/test_web.py` asserts the only recurring timers are the two status polls. A screenshot poller means a new timer, and a poller is a slow remote desktop.
- The same file asserts no page ever builds DOM from a string.
- `.github/workflows/ci.yml` greps for desktop-capture APIs and `Page.startScreencast` — the latter being the one route to video that never leaves CDP.

*Accept (unrun on hardware):* `roomctl shot -o wall.png` against the Pi, and confirm the image matches what is on the monitor at the size reported. Nothing in the suite can prove a picture *looks* right — only that the clip, the clamp and the wake behaviour around it are correct.

**v1.1.8 — inspect, input, and a rollback that can see the wall.** Done. Additive to the frozen `/v1`.

**`GET /v1/inspect`** is `/v1/screenshot` for a program, which cannot look at a picture: title, ready state, scroll position, form fields, and `error_page`. That last one is the point — Chromium's own crash and network pages render perfectly and answer `/v1/status` with a 200, so "Aw, Snap!" was indistinguishable from success everywhere in this API. It reports no field **values**: naming a password box is how a caller knows where to type, and handing back what is in it would turn a diagnostic into a credential leak.

**`POST /v1/input`** — click, double, right, move, drag, type, key, wait — exists for the failure a keyboard-less box cannot otherwise recover from: PLAN §6 has always said persisted cookies *reduce, not eliminate,* school re-logins, and until now an expired login meant a wall stuck on a form nobody could fill in.

- **It ships off**, behind `[interact] enabled` in `config.toml` — the root-owned file the agent cannot write. It is the only route here that acts *as* whoever the kiosk is logged in as; everything else shows something or reads something back. Off, `input` is absent from `supports` and the route 501s, so a client hides the feature rather than discovering it by failing. That reuses the capability mechanism exactly, and needed no new error semantics.
- **A list per request, not a route per verb.** A login is five actions; as five requests that is five websockets to the debug port and five chances to interleave. One request is one connection, one ordering, one audit line.
- **Structure is checked before anything runs; runtime failures stop the rest.** There is no undo. A typo in action 3 must not be found out after actions 1 and 2 have clicked and typed — and a click that missed must not be followed by a password typed into whatever else has focus. `/v1/extensions` set the precedent: the caller's typo is total, a runtime failure is per-item.
- **The audit line records that text was typed and how much, never what.**
- Deadline per request, default 30 s, capped by config; a caller may ask for less, never more.
- Still the page and nothing else: same CDP target as `navigate`, so it cannot alt-tab, reach the window manager, close the kiosk, or type into another application.

**The web UI** grew the payoff of pinning captures to `scale: 1` — **click the picture to click the page**, because the two are the same coordinate space. Type box with a *hide* toggle for passwords, ⏎ to submit, and a fresh capture after every action so you watch the form fill in. `imagePoint()` is its own function with no DOM in it, and `tests/test_web.py` runs it under node across five geometries: a wrong scale factor misses every target by a constant and looks exactly like the click never arriving, which is not something anyone can eyeball.

**`update.sh` can finally see the wall.** Step 6 proved the agent answers, which a Chromium error page does perfectly. It now also polls `/v1/inspect` and rolls back on `error_page: true` — *leniently*: inspect not answering at all (an older agent, a firefox box, a browser still coming up) is "cannot tell", never a rollback, because a false rollback on a keyboard-less box is worse than the regression it would be guarding. On any failure it saves a jpeg of what the wall was showing beside the `.failed-$TAG` latch. **Diagnostic, never a gate** — no pixel heuristic is worth a false rollback here.

`tests/test_deploy.py` is new and pins what `deploy/` assumes about the agent: the Pi has no `jq`, so `update.sh` reads JSON with `sed` and `case`, which makes the compact `"error_page":false` wire shape a contract between two files that never import each other. It also runs `bash -n` over every shell script, which nothing did before.

*Accept (unrun on hardware):* with `[interact] enabled`, put a login page on the wall, press Look, click the username box in the picture, type, and watch the next capture show the caret in the right field.

**v1.1.12–13 — the controller became a viewport.** Done, and no server change: every route it calls already existed.

The page was a form — a drop zone, then stacked fieldsets of buttons, and the screenshot bolted on underneath as one more panel. Once the capture existed that ordering was backwards: the picture of the wall is the thing you look at, and everything else is chrome around it. So the page is now an app shell — top bar, full-bleed capture, right-hand rail, bottom input bar — and the screenshot is the page rather than a feature of it.

- **The capture is the drop target.** Drop a file on the picture of the screen and it goes on that screen.
- **The rail** holds what you press repeatedly: scroll, auto-scroll and speed, window, display power. A column rather than a floating dock, because permanently covering a strip of the screen you are trying to read is the opposite of the point — the media dock may overlay only because it is there just while something plays.
- **`Ctrl-K` / `⋮`** is one filterable command list for everything else. Commands carry a `when` guard, so a firefox agent never sees the ones it would 501 on — the same `supports` mechanism, expressed as a list instead of a dozen `hidden` assignments.
- **The rail and the palette call the same named `ACT.*` functions.** A button that drifts from its palette entry is a bug nobody notices until the two disagree.
- Auto-scroll and display-power buttons read their state from `/v1/status`, never from a local toggle: the 04:00 restart drops a running autoscroll, and a latched button would go on claiming it.

Three bugs found while building it, each worth more than the feature that surfaced it:

- **Fresh captures were marked stale instantly.** `markStale()` was hooked into `req()` on "any non-GET" — but non-GET is not the same as changed-something. `probeMedia()` reads the player with a POST and runs on the 15 s poll, so every capture was stale the moment it was taken and again every fifteen seconds. Moved to `act()`, which wraps exactly the user-initiated actions.
- **The picture blinked on every swap.** Not the DOM clearing — the browser decoding the data url *after* the `<img>` was already in the document. `await img.decode()` before insertion, `aspect-ratio` to reserve the box, and `loading="lazy"` dropped: the bytes are already in the reply, so lazy only deferred the decode being avoided.
- **Captures landed mid-load.** `readyState: "complete"` means sub-resources loaded, not painted — and it reads `complete` for the *old* document until a navigation commits, so polling too early got a confident answer about the wrong page. Three constants now: a wait before asking, a grace after it reports complete, and a ceiling.

`tests/test_web.py` grew with it: the id check now counts a CSS reference as a reference (a layout hook used only by the stylesheet is still used), the timer rule holds `setInterval` to a fixed list *and* checks no recurring callback reaches `/v1/screenshot`, and `imagePoint()` — picture coordinates to page coordinates — runs under node across five geometries, because a wrong scale factor misses every target by a constant and looks exactly like the click never arriving.

**Future (post-v1) — native C# app.** A tray/hotkey client codegen'd from `/openapi.json`. **No server change.**

**Future considerations.** Deliberately deferred, each with the trigger that should bring it back. Not a wish list — if the trigger doesn't happen, the item is correct as unbuilt.

| Deferred | Add when |
|---|---|
| `roomctl display on\|off` | you want the display off from a terminal, or **eve** (Phase 7) needs it as an intent — eve imports `roomctl`, so that is where it lands |
| Monitor hotplug re-detection | you actually replug a monitor while the agent runs; today `detect()` runs once at startup and a swap needs a restart. Natural pairing with the v1.1.0 screens editor, which wants a "re-detect" button anyway |
| A Wayland backend for `display.py` | you move back to labwc. Swap `xset dpms force` → `wlopm --off/--on`, `xrandr --listmonitors` → `wlr-randr`; it gains per-monitor power for free. Not written now because it could not be tested — the box is X11 |
| Per-monitor power on X11 (`xrandr --output X --off`) | one monitor really does idle for hours while the other works. Costs a layout reflow, a `browser.place()` and the scroll position on wake |
| Quiet hours / clock-based off | the monitors are still on at 2am despite both timeouts |

---

## 8. Auto-update design (Phase 8 detail)

**Model:** the Pi **pulls**; GitHub never reaches in (no open ports, works behind Tailscale/NAT). A systemd **timer** checks for a new **release tag** ~every 30 min and deploys only tags you cut.

**Two halves:**
- **CI (GitHub):** `.github/workflows/ci.yml` runs on push/PR to main — lint + tests for `agent/` and `roomctl/`. A green run is your signal it's safe to tag. *(Starts as import/smoke tests; grows with your suite.)*
- **CD (Pi):** `update.sh`, driven by `crossdrop-update.timer` → `.service`.

**`update.sh` flow** (writes only when there's genuinely a new tag → SD-friendly):
1. **Cheap check:** `git ls-remote --tags` (read-only, over the deploy key) → highest semver tag. Equals the running tag? Exit 0, no writes.
2. **Fetch tag** into a new `releases/<tag>/` (cached clone + `git archive`, so no per-release `.git`).
3. **Build:** per-release venv, `pip install -r requirements.txt`.
4. **Health-check (boot sanity, no live port):** `python -m agent selfcheck` from the new venv — loads config, imports, boots the app in-process (Starlette `TestClient`), asserts `/v1/status` responds. Catches syntax/import/dep/config breakage without touching the running instance. Exit 0/1 gates the swap.
5. **Swap (atomic):** record current target as `previous`; repoint `current` symlink (`ln -sfn`); `systemctl restart crossdrop-agent`.
6. **Post-restart verify (real integration):** poll the live `/v1/status` ~30 s. Not healthy → **rollback**: point `current` back to `previous`, restart, log loudly to journald. This is what catches runtime/browser regressions that boot-sanity can't.
7. **Prune:** on success, keep the last 3 releases.

**Runtime layout on the Pi (separate from the repo, never overwritten by updates):**
```
/opt/crossdrop/
├── cache-repo/            # single clone, fetched --tags
├── releases/<tag>/        # per-release code + venv
└── current -> releases/<tag>
/etc/crossdrop/config.toml   # token, paths — NOT in the repo
<data dir>/                     # cookie snapshot + uploads (tmpfs-backed)
```
`crossdrop-agent.service` points at `/opt/crossdrop/current` and reads config from `/etc/crossdrop/` — updates swap **code only**, never your token, cookies, or uploads.

**Auth:** a **read-only deploy key** (SSH, single repo, revocable) on the Pi — not a personal PAT. Using `git ls-remote`/SSH keeps the deploy key as the only credential (no API token needed).

**Prereqs:** repo **private** (A6); add the `selfcheck` subcommand in this phase; tags follow semver (`v1.0.0`).

**Docs to pull at build (verified links provided then):** systemd timer/service units, `git ls-remote`/`git archive`, GitHub deploy keys, log2ram.

---

## 9. Storage / wear (summary)
- **Pi (SD):** profile + uploads on tmpfs; restore-on-boot, snapshot-on-stop (± hourly). Cache and uploaded refs are disposable → near-zero idle writes; only auth state persists. `update.sh` writes only on a real new release.
- Power-loss choice: (a) snapshot at stop only — fewest writes, may lose last session; (b) + hourly timer — a few small writes/day, survives power-loss. Default (a).

## 10. Security checklist
- [ ] Bearer token on all `/v1` routes; per-deployment secret, git-ignored.
- [ ] FastAPI binds to the tailnet interface, not broad `0.0.0.0`.
- [ ] `:9222` stays on `127.0.0.1`.
- [ ] Upload: size cap, type allowlist, filename sanitize, dedicated dir.
- [ ] `/v1/navigate` URL-scheme allowlist (`http`,`https`).
- [ ] `/v1/extensions` takes Web Store **ids**, never urls; id regex + size cap
      before the fetch. It installs executable code — see §11.
- [ ] Snapshot archive perms `600` (session tokens).
- [ ] Repo **private**; **read-only** deploy key; branch/tag protection + 2FA on the account.
- [ ] Ship `.example` configs only.

---

## 11. Adversarial review — standing decisions and what is still open

Full review of `agent/`, `roomctl/`, `web/`, `deploy/` on 2026-09-01, against a
stated trust boundary of **the tailnet only** (no hostile device on it) on a
**4 GB Pi**, with the auto-update timer treated as something that will be
enabled. Eleven findings. The fixes are in the code and their tests; what
follows is only what a reader still needs — the decisions not to fix, and the
things nobody has confirmed on hardware.

**Fixed:** updater redeploy loop (`.failed-$TAG` marker in `update.sh`);
`--remote-allow-origins=*` deleted; tmpfs budget (`keep` 20 → 5, and `save()`
sweeps before the write, which is what unwedges a full tmpfs); `keep = 0`
floored at 1; `cfg.clear()` torn read; `nosniff` on `/files/{id}`; autoscroll's
per-tick websocket (see Phase 7a).

**Deliberately not fixed.** Each costs more than the risk it removes — do not
"fix" these without a new reason:

| # | Finding | Why it stands |
|---|---|---|
| 8 | Bearer token on `update.sh`'s `curl` command line, visible in `/proc/<pid>/cmdline` | The threat is another local user on a Pi that has exactly one. `-H @-` if that ever changes. |
| 10 | Auto-update deploys unsigned tags — push access to the repo is code execution on every Pi within 30 min | Trades a permanent release-signing burden against a path that ships **disabled** and is opt-in. `git verify-tag` if this runs on a box that matters. |
| 11 | `request.url_for` builds the post-upload URL from the client's `Host` header | Needs a proxy or a hand-crafted `Host` on an already-authenticated route. Bites a program harder than a person; build it from the configured bind address if it ever does. |

**Later, and decided on purpose.**

- **`POST /v1/extensions` installs browser extensions** (v1.3). This is the only
  route that puts **executable code** on the box, and an extension with broad
  host permissions can read every page the kiosk shows — including the logged-in
  pages whose session tokens `profile-snapshot.sh` exists to keep. Nothing else
  in this API can do that. It ships anyway, inside the stated tailnet boundary
  and behind the same bearer token, constrained so that it is an *installer* and
  not a fetcher: the body carries **Web Store ids only, never a url**; the url
  is a fixed template; ids are checked against `^[a-p]{32}$` before anything is
  requested; the download is capped at 50 MB; and the archive is unpacked into a
  staging directory that `extractall` cannot escape. Revisit if the trust
  boundary ever widens beyond "the tailnet, no hostile device on it" — the
  honest fix then is an allowlist of ids in `config.toml`, which the agent
  cannot write.

**v1.2.0 — which window is which.** Done. `_cdp_page()` mapped screens to windows by *position in `/json`'s list*. That is the order the windows were opened in — but only while the list holds nothing but those windows, and a browser with extensions loaded does not guarantee it. One stray page target moved every screen one place along, silently, and the wrong mapping was then written into `_targets` so it stayed wrong until a restart. Tolerable when the worst case was a navigate on the wrong monitor; not tolerable now that the same path carries a click and a typed password.

Three changes, in order of how much they do:

- **Drop the targets no window of ours could be showing** — `devtools://` and `chrome-extension://`, and *only* those, because `/v1/navigate` allows http and https alone and `home_url` is validated the same way, so nothing can steer a kiosk window there. `chrome-error://` is deliberately kept: that is our own window having failed to load, which is the exact state `/v1/inspect` reports and `update.sh` rolls back on, so filtering it would lose the window at the moment it most needs describing. (Written the other way first, and `tests/test_input.py` caught it.)
- **Index only while the counts agree.** Same window count as screens, and list order is meaningful; otherwise it is not, and position in a list is no basis for deciding which monitor gets the next keystroke.
- **Otherwise ask the browser where its windows actually are** (`Browser.getWindowForTarget`) and match against the screen's own coordinates — nearest, since a compositor may nudge a window a few pixels. Nothing to match on means a `RuntimeError` the caller sees as a 503, and **nothing is cached**: the old failure was not picking wrong once, it was recording the wrong answer and repeating it unasked.

`tests/test_screens.py` covers all of it; each of the nine fails against the old implementation.

**Closed since.** Every code and installer finding above has been dealt with:

| Was | Now |
|---|---|
| 9 — `tailscale up --ssh` enabled silently | Announced before it happens, and declinable with `TSSSH=0` |
| `apt full-upgrade` can turn 3 minutes into 30 | `UPGRADE=0` skips it, and the wait is announced |
| Nothing checked `pip install` succeeded | Checked, and it refuses to enable the service |
| The banner printed the token into scrollback | The token is never read into a variable; the banner prints the command that reads it |
| 10 — unsigned tags are code execution on every Pi | `VERIFY_TAG=1` makes `git verify-tag` a hard gate. Off by default: enabling it without a key in place would stop every Pi updating, and a display stuck on an old release is worse than the risk it removes |
| S3 — a wedged browser stacks up threadpool threads | Fail-fast latch in `_get()`, `CROSSDROP_DEAD_COOLDOWN`. `wait_ready()` bypasses it, or a launch poll would go from 0.3 s to 5 s |
| S9 — a non-ASCII token 500s | Compared as bytes, so it 401s; and `load_config` warns that such a token can never be sent at all |
| S10 — the sweep could evict the upload just written | `sweep(spare=…)` names it rather than trusting mtime order |
| S11 — `up` is a hardcoded `true` | Left alone and documented: it means "this agent answered", `browser`/`error` carry the news. Redefining a frozen field would silently change behaviour for anything that does read it |

Two leaks surfaced while fixing those, both mine, both from this session's work:

- **`_launch()` re-read `app.state.stopping` each pass**, so a thread from a previous lifespan saw the *next* one's fresh unset event and carried on launching browsers for an agent that had already stopped. One process has one lifespan, so it could never bite in production — it bit the suite, which starts dozens. The event is handed to the thread now.
- **Two autoscroll tests passed on a race.** They left `browser.autoscroll` unstubbed, so the worker reached a debug port that was not there and removed its own entry; the assertion only won because that failure took about two seconds. The S3 latch made it instant and the race flipped. Stubbed properly, and they wait for the state rather than assuming it.

**Still open, and not code.**

- **Two things unverified on hardware.** Removing `--remote-allow-origins=*`
  only matters against a real Chromium — no test covers it and none can; on the
  next deploy confirm `/v1/status` still returns a `current_url` rather than a
  browser error. And autoscroll's connection count is proven but its CPU cost is
  not: run `roomctl autoscroll start --speed 40` on a long page, leave it two
  minutes, watch `%CPU` in `top`. It now issues two `synthesizeScrollGesture`
  calls a second rather than ten wheel events, so the round-trip half of that
  cost is five times smaller — but Chromium is doing the interpolation instead,
  and nobody has measured which side that lands on. Same run answers it.
- **The smoke suite has never run on the Pi**, only against a desktop Chrome.
  `deploy/pi/update-over-ssh.md` §8 is the command.
- **The rollback has never actually fired** since `update.sh` grew the
  `/v1/inspect` gate and the failure screenshot. It is the one feature here that
  matters and it is still only proven on the happy path.
- **Naming — done, v2.0.0.** **CrossDrop is the repo and the installation.**
  `roomctl` stays the client.

  This section used to argue the opposite: that `room-display` was the
  installation, that two names were deliberate, and that a flag day was "a
  migration on hardware nobody can reach with a keyboard, for a cosmetic gain".
  That argument was wrong in its premise. There were never two names — there
  were **three**. The unit was `display-agent`, which is neither the repo name
  nor the install name, and it is the one you type most: `systemctl --user
  status crossdrop` is the obvious guess and it used to fail, while
  `systemctl --user list-units 'room-display*'` listed the three timers and
  missed the agent itself. That is not cosmetic, it is the thing you reach for
  at the moment something is broken.

  What changed the cost side was `tests/test_install_roundtrip.py`. The old
  argument priced a migration as unverifiable, and it was: nothing ran the
  install or the uninstall, so the only way to find out was on hardware. The
  round-trip harness runs both against a temp tree with stubbed `sudo`, `apt`
  and `systemctl`, so the migration is covered by tests that fail in CI rather
  than on a wall.

  How it was done, and why each piece is where it is:

  - **`deploy/pi/migrate.sh`, not `update.sh`.** `update.sh` cannot rename its
    own root, for four independent reasons, any one fatal: the update timer's
    `ExecStart` is frozen at the old path (update.sh has never rewritten unit
    files, only setup.sh does); bash reads a script as it runs, so `mv` truncates
    everything after it *including the rollback block*; `WorkingDirectory=`
    breaks the restart that rollback itself issues; and reloading the unit set
    from inside a unit in that set is a knot.
  - **The code is disposable; the state is not.** `migrate.sh` never moves
    `/opt` — it moves the token and `profile.tar.gz` (the browser logins), backs
    the config up beside itself, and hands off to `setup.sh`, which is
    idempotent and is the half with the round-trip test behind it. `/opt` is
    rebuilt from git because it is a checkout and nothing else.
  - **`ROOM_*` → `CROSSDROP_*`, no compatibility fallback.** A fallback would
    make the half-migrated state *work*, which means nobody migrates and the
    fallback is permanent. `agent/selfcheck.py` refuses instead, and because
    `update.sh` gates the swap on selfcheck, an unmigrated Pi that reaches the
    v2 tag **does not swap** — it keeps running the release it has, stays
    healthy, and prints the migrate command into its own journal.
  - **`uninstall.sh` knows both layouts.** After a migration the old
    uninstall.sh is gone from the box, so the new one has to be able to clean a
    Pi that never migrated.
  - **`roomctl` is not renamed.** It is the client, not a deploy. Renaming it
    breaks `pyproject`, the console script, the Windows tray app's relative
    path resolution, and — worst — `roomctl/__init__.py`'s default targets
    path, which means a user's bearer tokens live inside the installed package
    and would be silently orphaned. Product plus tool is a normal shape:
    docker/docker-compose, git/gh.

  The data-dir fix the old text describes still stands and is unchanged:
  `settings.DATA_DIR`, one `CROSSDROP_DATA` that moves the agent and
  `profile-snapshot.sh` together, and a test that fails if the two defaults
  drift.

  Still unchecked: `setup.sh` hardcodes `github.com/Kramav/CrossDrop`, so if
  that repo is private both the README's `curl | bash` line and the clone fail
  on a git credential prompt in a pipeline with no tty.

## 12. Open items for you
1. Confirm A2 (SD boot), A4 (64-bit OS), A5 (desktop auto-login), A6 (private repo).
2. Power-loss frequency on the Pi (Phase 6 snapshot cadence).
3. Upload size cap (e.g., 25 MB) — sets the tmpfs guard.
4. Before Phase 8: create a read-only **deploy key** for the repo and add it to the Pi.
