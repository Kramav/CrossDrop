# NEXT-STEPS — living roadmap

**Living document.** Update it when a decision is made, not when a release is
cut. If you had to think about something for more than five minutes, the
conclusion belongs here.

Three files, three jobs, no overlap:

| File | Holds | Lifecycle |
|---|---|---|
| [PLAN.md](PLAN.md) | **Why** it is built this way. Architecture, the frozen `/v1` contract, the per-version record, §13 product direction. Cited by section number from code comments. | Append-only |
| [DEBT.md](DEBT.md) | Review findings **deliberately not fixed**, each with what would make it worth doing. | Rewritten per review round |
| **NEXT-STEPS.md** (this) | **Where it stands, what is open, what is next.** The one file to read on a Monday. | Living |

Last updated **2026-09-12**.

---

## Where it stands

Suite green on this machine: **394 passed, 21 skipped**, 184s. The skips are the
`CROSSDROP_SMOKE=1` browser tests (kiosk and extension), which CI runs in their
own job. The 7 extension tests also pass under `CROSSDROP_SMOKE=1` on Chrome
152 and Edge 153 here.

What ships today, in one paragraph: one FastAPI agent on a Pi owns a kiosk
browser and the monitors, and exposes a frozen `/v1` HTTP API. You can send it
a URL or a file, split a monitor into independent screens, read back what is
actually on the wall (`/v1/screenshot`, `/v1/inspect`), scroll it, drive its
video, click and type into the page, power the monitors, install ad blockers,
and edit screen names and home pages live. Four clients: the web UI it serves,
a Windows tray app, `roomctl` (CLI + Python library), and a Chrome/Edge
extension (M1, built, not yet accepted on a real browser). It updates itself
from release tags and rolls itself back if the new one shows an error page.

What does not exist: **device discovery, pairing, more than one device, and tab
mirroring.** That is the whole of what is next — see
[PLAN.md §13](PLAN.md#13-product-direction--the-industry-review) for the
reasoning and [What's next](#whats-next) for the tasks.

---

## Open

Nothing here is a bug. These are things that are true, unverified, or owed.

### Never verified on hardware

Every one of these needs the Pi and cannot be closed from a dev box. The suite
cannot substitute for any of them — that is why they are still here.

- [ ] **`--remote-allow-origins=*` is gone; no test can prove it was safe to
  remove.** On the next deploy, confirm `/v1/status` still returns a real
  `current_url` rather than a browser error. If it does not, the CDP origin
  check is rejecting us (PLAN §6, §11).
- [ ] **Autoscroll CPU cost.** `roomctl autoscroll start --speed 40` on a long
  page, leave it two minutes, watch `%CPU` in `top`. The connection count is
  proven; the cost of handing interpolation to Chromium is not measured.
- [ ] **The smoke suite has never run on the Pi**, only against a desktop
  Chrome. Command and walkthrough: `deploy/pi/smoke-on-the-pi.md`,
  `deploy/pi/update-over-ssh.md` §8. The agent must be stopped first.
- [ ] **The rollback has never actually fired** since `update.sh` grew the
  `/v1/inspect` gate and the failure screenshot. It is the single most important
  behaviour in `deploy/` and it is proven only on the happy path. Drill: tag a
  deliberately broken commit, watch it decline and stay on the old release.
- [ ] **`VERIFY_TAG=1` accept path.** The refusal is tested against a real
  unsigned tag; signing needs a key on the box.
  `deploy/pi/smoke-on-the-pi.md` § "What this does not cover" has the drill.
- [ ] **Screens editor (v1.1.0 accept).** Move a window between monitors from
  the UI, no restart, and have it survive one.
- [ ] **Agent outlives its browser (v1.1.7 accept).** Rename the Chromium
  binary, restart the agent, read the reason out of `roomctl status` from a
  controller box rather than from a keyboard.
- [ ] **Input on a real login (v1.1.8 accept).** With `[interact] enabled`, put
  a login page on the wall, press Look, click the username box in the picture,
  type, and confirm the next capture shows the caret in the right field.
- [ ] **`/files` CSP.** Load a PDF and a video through `/files` with a bare
  `sandbox` (drop `allow-scripts`). If PDFs still render, drop it for good — it
  buys nothing today, and the claim that it is needed is believed, not measured.
  The boundary itself rests on `test_nothing_in_types_can_execute`, not on the
  header.

### The record has drifted

Four things ship and are documented only in `agent/config.example.toml`. Fix by
writing them where a reader would look, not by writing them twice.

- [x] **`allow_extensions` was described as unbuilt in the security section.**
  PLAN §11's extension entry read "*the honest fix then is an allowlist of ids
  in `config.toml`*" in future tense; it ships. §11 now carries a **Since
  built** note. Still absent from README — worth a line wherever
  `/v1/extensions` is described, since it is the only route that puts
  executable code on the box.
- [ ] **`restore_within_minutes`** — puts each screen back on what it was
  showing after the nightly restart. In neither PLAN.md nor README.md. This is
  the feature that makes the 04:00 restart invisible; it deserves a line.
- [ ] **`disk_cache_mb`** — undocumented outside the example config, and it is a
  RAM cap on a box where the profile is tmpfs.
- [ ] **Screen splits and the separate token file have no PLAN.md entry.** Both
  are in README; the per-version record in §7 skips them, along with the capture
  cache. Add short entries so §7 is a complete history rather than a partial one.

### Decisions owed

- [ ] **Confirm A2 (SD boot), A4 (64-bit Pi OS), A5 (desktop auto-login), A6
  (private repo)** — PLAN §1. Four one-word answers that several design choices
  rest on.
- [ ] **Power-loss frequency on the Pi.** Decides whether the profile snapshot
  stays stop-only (default, fewest SD writes, may lose the last session) or gains
  the hourly timer. PLAN §9.
- [ ] **Read-only deploy key on the Pi**, if the auto-update timer is ever to be
  enabled. PLAN §8.
- [ ] **`setup.sh` hardcodes `github.com/Kramav/CrossDrop`.** If that repo is
  private, both the README's `curl | bash` line and the clone hit a git
  credential prompt in a pipeline with no tty. Unresolved because A6 is
  unconfirmed — same question as above.

### Deliberately not doing

Live in [DEBT.md](DEBT.md), with the trigger that would change the answer. Not
repeated here. As of the 2026-09-08 round: three Medium, four Low, five
accepted-not-debt. The stop rule was never met — round 4 still returned a High
— so a fifth adversarial round is available if anything in that area is touched.

---

## The record — what was built, and why

The one-line version. PLAN.md carries the full reasoning for each; the section
column is where to look.

| Version | What | The reason it exists | Where |
|---|---|---|---|
| v1.0.0 | Agent, frozen `/v1`, `roomctl`, web UI, uploads, Pi provisioning, tmpfs profile, release-gated auto-update | One server, many clients: adding a control surface must never touch the server | PLAN §7 P0–P8 |
| v1.0.1 | Agent-owned display power (DPMS claimed at startup, idle timers, any `/v1` call wakes it) | **The Pi has no keyboard.** Anything that blanks the screen and wakes only on input is unrecoverable without unplugging the box | PLAN §7 |
| v1.1.0 | Screens editor in the web UI → `settings.json` | Editing a screen name should not be `ssh` + `nano` + restart. JSON not TOML because `tomllib` only reads | PLAN §7 |
| v1.1.2 | `/v1/media` — play, pause, ±10s, mute, volume | A display that can show a video could not pause one | PLAN §7 |
| v1.1.7 | Agent survives a browser that will not start; autoscroll lock; one log line per request | A failed launch used to take the API down with it, so the one thing that could name the cause was never up | PLAN §7 |
| v1.1.7 | `/v1/screenshot`, pinned to `scale: 1` | Every other route reports the URL it was *given* — a redirect, an expired login and a crashed tab all looked like success. CSS pixels because it is also the coordinate space input needs | PLAN §7 |
| v1.1.8 | `/v1/inspect`, `/v1/input` (ships off), `update.sh` rolls back on `error_page` | An expired SSO login is a page nobody can get past, and it is the one failure a keyboard-less box cannot otherwise recover from | PLAN §7 |
| v1.1.12–13 | Web UI became a viewport — the capture *is* the page, rail, ⌃K palette | Once the capture existed, a form with a screenshot bolted underneath was the wrong way round | PLAN §7 |
| v1.2.0 | Window→screen mapping by coordinates, not list order | One stray page target moved every screen one place along, silently. Tolerable for a navigate; not for a typed password | PLAN §11 |
| v1.3.0 | `roomctl` by-name module functions deleted (**breaking**) | 86 lines of pure delegation; every new endpoint had to be written in three places | README |
| v2.0.0 | One name: CrossDrop. `migrate.sh` | There were three names, and the one you type most (`display-agent`) was neither the repo nor the install | PLAN §11 |
| *unversioned* | Screen splits, capture cache, `restore_within_minutes`, `allow_extensions`, separate token file | See [The record has drifted](#the-record-has-drifted) — these need entries | — |

**The two facts that explain most of the code**, worth restating because every
future decision runs into them: the Pi has **no keyboard**, and the browser is
driven **remotely** (CDP for Chromium/Edge, WebDriver BiDi for Firefox — so
Firefox `501`s on scroll, media, screenshot and screens 2+).

**Two frozen warts** stay wrong on purpose: `Status.up` is always `true`, and
`/v1/autoscroll` puts a screen *name* in `current_url`. `/v1` is frozen and a
client reading either would change behaviour the day they were fixed.

---

## What's next

From [PLAN.md §13](PLAN.md#13-product-direction--the-industry-review), which
evaluates `industryreview.txt` against what exists. The short version: the
review's Phases 1, 5 and 11 already shipped; Phases 3, 4, 6 and the device half
of Phase 2 are the real gaps, and the first of them needs **no server change**.

The measure of success is the review's own, and it is a good one:

> Can a user open a webpage, right-click it, select "Living Room," and have the
> page appear on the other machine in one or two clicks?

### M1 — Send this tab ← built, awaiting acceptance

A Chrome/Edge MV3 extension that calls `POST /v1/navigate` (and `GET
/v1/status`, to test a saved display) and nothing else. No server change.
[extension/README.md](extension/README.md) has install and use.

- [x] `extension/`: `manifest.json` (MV3), service worker, popup.
  **Corrected:** "one `host_permissions` entry for the tailnet range" cannot be
  built. A match pattern has no CIDR, so the tailnet is either `http://*/*`,
  an install warning for every website, or nothing. Shipped instead:
  `optional_host_permissions`, with a grant for the one origin requested
  when a display is saved. Nothing is asked at install.
  `test_manifest_asks_for_nothing_at_install` keeps it that way.
- [x] Context menu on page and on link; toolbar popup; <kbd>Alt</kbd>+<kbd>Shift</kbd>+<kbd>D</kbd>.
- [x] Badge ✓ (clears) / ! (stays until the popup opens), with the reason in the
  icon's hover title. roomctl's vocabulary: unreachable, 401 token, 501, 503
  browser down. A missing grant is named separately, because otherwise it
  fails exactly like a box that is off.
- [x] Token and URL in `chrome.storage.sync`, entered by hand; M3 automates it.
  Stored as a one-entry `devices` list already, so M2 adds entries rather than
  migrating the schema (PLAN §13 decision 3).
- [x] **Freethrow bridge**, beyond the plan and off by default: a popup switch
  that PUTs each window's active tab to `127.0.0.1:47800`, so Freethrow can
  turn a window handle into a URL. Contract in the extension README.
- [x] `tests/test_extension.py`: 2 static tests, plus 5 that load the extension
  into headless Chrome/Edge via CDP `Extensions.loadUnpacked` and drive the
  service worker against a real agent. Added to CI's smoke job. Branded Chrome
  has ignored `--load-extension` since 137. That was checked here first, and it
  loads nothing, silently.

**Both assumptions verified on 2026-09-12**, from a headless service worker on
Chrome 152 and Edge 153:

- Against the real Pi, read-only `GET /v1/status` with the bearer token: **200
  with the host permission** (`{"kind": "chromium", "browser": "ok", "error": ""}`),
  `TypeError: Failed to fetch` without it. With the grant the agent sees no
  preflight and no `Origin` on a GET. Without it Chrome sends `OPTIONS` and the
  agent 405s. **No CORS middleware.**
- Plain `http://` to a `100.x` tailnet address is allowed. Local Network Access
  did not block it.
- One trap worth knowing: on Windows, a killed Chrome/Edge launcher can leave
  headless children holding the debug port. The next launch then silently talks
  to the *old* browser. It briefly made "no permission" look like a 200. The
  test tears down with `browser.stop`, which kills the tree.

- [ ] **Accept on a real browser.** Load unpacked, save the Pi, right-click a
  page → the wall shows it. Also check the one step a headless test cannot
  reach: the per-origin permission prompt on **Save and test**.

*Accept: right-click a page → Send to → the wall shows it. Two clicks, no
terminal, no `targets.toml`.*

### M2 — Devices have names

- [ ] Device list in `chrome.storage.sync`: `{name, url, token, last_seen}`.
  **Client-side only** — no server registry and no hub; see §13 decision 1.
- [ ] Online dot per device from `GET /v1/status`, screen picker from the same
  reply's `screens`.
- [ ] Default destination, and recents.

*Accept: two devices listed, one unplugged, and the popup says which is which
before you click.*

### M3 — Pairing

- [ ] Agent advertises `_crossdrop._tcp` (mDNS) for clients that can listen —
  `roomctl`, the tray app.
- [ ] **Pair** panel in the agent's web UI emitting a blob the extension
  accepts.

> **Known constraint, do not design around it:** MV3 has no mDNS API.
> `chrome.mdns` was Chrome-Apps-only and is gone. The extension **cannot**
> discover anything on its own — hence the web-UI handoff. PLAN §13 decision 2.

*Accept: add a display to the extension without opening a text editor or
reading an IP address aloud.*

### M4 — Mirror this tab

- [ ] `chrome.tabs.onUpdated` on one tab → `/v1/navigate` on URL change.
- [ ] An explicit **stop** that leaves the remote page where it is.
- [ ] One additive server change: `POST /v1/scroll` accepts `{"y": <int>}` for
  an absolute position, alongside the existing `dy` and `to`. Additive, so `/v1`
  stays frozen.

*Accept: navigate three pages locally and watch the wall follow; press stop, and
a fourth navigation does not move it.*

### M5 — Rooms

- [ ] A room is a named list of `(device, screen)` pairs, client-side, fanned
  out by a loop — the same shape as the web UI's client-side `"all"` for
  screenshots. No server change; per-target results reported the way `screens[]`
  already reports per-screen.

*Accept: "All Screens" puts one URL on every monitor of every paired box, and
names the one that was off.*

### Later, and not blocking anything above

Send a screenshot of the local tab; smart content handling one case at a time;
session handoff beyond URL + scroll; a phone client.

**Refused on purpose** — pixel mirroring, a cloud relay, a content-detection
framework, full session migration, a scheduler inside the agent. Each has its
reasoning and its trigger in PLAN §13 *Declined*. Do not re-raise without the
trigger.

---

## Keeping this current

- A decision made → the row or bullet it changes, same day. A *reversed*
  decision → say it was reversed and why, do not silently edit.
- An item verified on hardware → tick it and write what it printed. "Verified"
  with no output is how the CI smoke gap lasted.
- A milestone finished → move it into **The record** with its one-line reason,
  and add the PLAN.md version entry.
- Something deliberately not fixed → **DEBT.md**, not here, with its trigger.
- Long-form architecture reasoning → **PLAN.md**, and link to it. Two copies of
  a rationale is the sync problem this repo has already complained about once.
