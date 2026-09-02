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
 │ Win10 desktop        │              │  display-agent (FastAPI)         │
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

```
room-display-control/
├── agent/
│   ├── app.py                # FastAPI: /v1/navigate /v1/upload /v1/status, GET /, /files
│   ├── browser.py            # launch kiosk + raw-CDP navigate
│   ├── storage.py            # tmpfs upload store (size/type caps, id map)
│   ├── selfcheck.py          # `python -m agent selfcheck` -> boot sanity (Phase 8)
│   ├── config.example.toml
│   └── requirements.txt      # fastapi, uvicorn, websocket-client, python-multipart
├── roomctl/                  # shared client lib (CLI + eve import this)
│   ├── __init__.py           # navigate(target,url), upload(target,path), status()
│   ├── cli.py                # `roomctl navigate <url>`
│   └── targets.example.toml
├── web/
│   ├── index.html            # drop-zone + paste + saved refs
│   └── app.js                # fetch() to /v1/*
├── deploy/
│   ├── pi/
│   │   ├── display-agent.service
│   │   ├── kiosk-launch.sh
│   │   ├── cookie-restore.sh
│   │   ├── cookie-snapshot.sh
│   │   ├── update.sh                  # release-gated pull + health-check + rollback
│   │   ├── room-display-update.service
│   │   ├── room-display-update.timer  # ~every 30 min
│   │   └── tmpfs-setup.md
│   └── github/
│       └── ci.yml            # Actions: lint + tests on push/PR to main
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
- `display-agent.service` runs **as the login user**, tied to `graphical-session.target`; `kiosk-launch.sh`; on-SD profile to isolate variables.
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

**v1.1.0 — screens editor in the web UI.** Built. Edit a screen's `position`, `size`, `home_url` and name from the drop-zone page instead of `ssh` + `nano` + restart, applied **live** via `browser.place()`. Persists to `~/.local/share/room-display/settings.json` — **JSON, not the planned `screens.toml`**, because `tomllib` only reads and a TOML writer is a new dependency for a file no human edits. `/etc/room-display/config.toml` stays un-writable by the agent as specified, and `token`, `profile_dir`, `upload.dir`, `debug_port` and `browser.kind` stay file-only. Blank `position`/`size` falls back to `display.detect()`, which is the re-detect path the UI exposes as a button. Same work put the token in a real field on the page instead of a `prompt()`. See `agent/settings.py`, `tests/test_settings.py`. *Accept (still unrun on hardware):* move a window between monitors from the UI, with no restart, and have it survive one.

**Deliberately still out of scope after v1.1.0.** Display sleep timeouts (`idle_off_minutes`, `content_off_minutes`) and upload caps (`max_mb`, `keep`) are runtime-safe and would drop into the same file in ~10 lines each; they were considered and left file-only. Add them when editing a config file over `ssh` is actually what stands in the way.

**v1.0.1 — agent-owned display power.** Done. The Pi has no keyboard, so anything that blanks the screen and wakes only on *input* can only be cured by unplugging the box. The agent claims DPMS at startup (timeouts zeroed, DPMS kept enabled) and drives power itself: idle on its home page → off after 10 min, showing a site → off after 2 h without a request, and **any `/v1` call wakes it**. `POST /v1/display` and a UI button cover leaving the room. Both monitors sleep together — X11 has no per-output power. Same commit made screens self-detecting (`xrandr --listmonitors`), so a fresh install needs no `[[screen]]` blocks written by hand. See `agent/display.py`, `deploy/pi/README.md` §8.

**v1.1.1 — playback control.** Done. A room display that can show a video could not pause one: the box has no keyboard, so whatever was pushed at it played to the end or not at all. `POST /v1/media` drives the page's own `<video>`/`<audio>` through one `Runtime.evaluate` — play, pause, ±10 s, mute, volume — with CDP's `userGesture`, which is what gets past Chromium's autoplay block on a page nobody can click. The web UI reveals its transport bar only when the screen really has a media element, the tray gets Play/pause, and `roomctl media` is the CLI. Uploads accept `.mp4 .webm .mp3 .m4a .wav` under the same tmpfs cap. Not covered: players inside cross-origin iframes (no execution context there) and the Pi's own ALSA volume. See `agent/browser.py` `media()`, `tests/test_media.py`.

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
- **CI (GitHub):** `deploy/github/ci.yml` runs on push/PR to main — lint + tests for `agent/` and `roomctl/`. A green run is your signal it's safe to tag. *(Starts as import/smoke tests; grows with your suite.)*
- **CD (Pi):** `update.sh`, driven by `room-display-update.timer` → `.service`.

**`update.sh` flow** (writes only when there's genuinely a new tag → SD-friendly):
1. **Cheap check:** `git ls-remote --tags` (read-only, over the deploy key) → highest semver tag. Equals the running tag? Exit 0, no writes.
2. **Fetch tag** into a new `releases/<tag>/` (cached clone + `git archive`, so no per-release `.git`).
3. **Build:** per-release venv, `pip install -r requirements.txt`.
4. **Health-check (boot sanity, no live port):** `python -m agent selfcheck` from the new venv — loads config, imports, boots the app in-process (Starlette `TestClient`), asserts `/v1/status` responds. Catches syntax/import/dep/config breakage without touching the running instance. Exit 0/1 gates the swap.
5. **Swap (atomic):** record current target as `previous`; repoint `current` symlink (`ln -sfn`); `systemctl restart display-agent`.
6. **Post-restart verify (real integration):** poll the live `/v1/status` ~30 s. Not healthy → **rollback**: point `current` back to `previous`, restart, log loudly to journald. This is what catches runtime/browser regressions that boot-sanity can't.
7. **Prune:** on success, keep the last 3 releases.

**Runtime layout on the Pi (separate from the repo, never overwritten by updates):**
```
/opt/room-display/
├── cache-repo/            # single clone, fetched --tags
├── releases/<tag>/        # per-release code + venv
└── current -> releases/<tag>
/etc/room-display/config.toml   # token, paths — NOT in the repo
<data dir>/                     # cookie snapshot + uploads (tmpfs-backed)
```
`display-agent.service` points at `/opt/room-display/current` and reads config from `/etc/room-display/` — updates swap **code only**, never your token, cookies, or uploads.

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
- [ ] Snapshot archive perms `600` (session tokens).
- [ ] Repo **private**; **read-only** deploy key; branch/tag protection + 2FA on the account.
- [ ] Ship `.example` configs only.

---

## 11. Kickoff prompt for Claude Code

> Scaffold the repo per PLAN.md §4 (FastAPI + uvicorn + websocket-client + python-multipart; `.example` configs; `.gitignore` for real configs, snapshots, uploads, releases). Then implement **Phase 1 only**: `browser.py` (launch local kiosk Chromium/Edge with `--remote-debugging-port=9222 --remote-allow-origins=* --kiosk --user-data-dir=<dedicated> --no-first-run`, then navigate the existing tab via raw CDP — GET `/json` for the WebSocket URL, send `Page.navigate`) and `app.py` (FastAPI `POST /v1/navigate` with bearer auth + http/https URL-scheme validation, plus `GET /v1/status`). Freeze the `/v1` request/response models per PLAN.md §5. Add a localhost smoke test that launches the browser, POSTs a URL, and asserts navigation. Do NOT build the client, web UI, upload, Pi deploy, or auto-update yet. Flag any place the CDP origin flag differs for my installed Chromium version.

## 12. Open items for you
1. Confirm A2 (SD boot), A4 (64-bit OS), A5 (desktop auto-login), A6 (private repo).
2. Power-loss frequency on the Pi (Phase 6 snapshot cadence).
3. Upload size cap (e.g., 25 MB) — sets the tmpfs guard.
4. Before Phase 8: create a read-only **deploy key** for the repo and add it to the Pi.
