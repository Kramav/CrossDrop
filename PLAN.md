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
- Auth: `Authorization: Bearer <token>` on all `/v1` routes.

Published by FastAPI at `/docs` + `/openapi.json`. **v1 semantics frozen** once the native app targets it.

---

## 6. Known gotchas / correctness flags
- **CDP origin check** — launch browser with `--remote-allow-origins=*` or CDP silently fails (Chrome ≥ 111). *Verify vs your Chromium version.*
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

**Phase 8 — Auto-update from GitHub (release-gated CD).** See §8 below for the full design. *Accept:* tag a deliberately broken commit → Pi's health-check fails → it stays on the previous good version (logged); tag a good commit → Pi updates within one timer interval and `/v1/status.version` shows the new tag.

**Future (post-v1) — native C# app.** A tray/hotkey client codegen'd from `/openapi.json`. **No server change.**

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
