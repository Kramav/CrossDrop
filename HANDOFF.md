# Handoff — 2026-08-28

State at end of session. `main` is at `312e750`; **73 tests passing + 2 skipped**
(was 49/2), `ruff check --select E9,F` clean (what CI runs).

**Uncommitted in the working tree** — nothing here is pushed yet:

    ?? deploy/windows/        the tray app, its README, its selfcheck
    ?? agent/settings.py      agent-owned runtime settings (the screens editor)
    ?? tests/test_settings.py  22 tests
    ?? tests/test_web.py       page selector check
    ?? tests/conftest.py       ROOM_SETTINGS isolation, autouse
     M agent/app.py           settings.apply() + GET/PUT /v1/settings
     M web/index.html         token field, screens editor, "All", footer
     M README.md              tray app, settings, contract table
     M PLAN.md                v1.1.0 marked built
     M HANDOFF.md             this file, including last session's rewrite

Two independent pieces of work sit here: the **tray app** (`deploy/windows/`,
no Python, commit it on its own) and the **screens editor** (everything else).

## Where the project is

Phases 1, 3, 4, 5, 6, 8 done. Phase 2 (`roomctl`) done. **Phase 7 (eve) is the
only phase never started**, and `roomctl` already unblocks it.

**v1.0.0 is tagged and running on the Pi.** The first tag deploy worked,
including the migration from the Phase 5 clone to the release symlink — the half
of Phase 8 that had never touched hardware. The rollback half still hasn't run
(see below).

The four v1.0.0 features are built **and verified on real hardware**:

1. Home screen — both monitors show it, each naming itself
2. Desktop GUI — screen picker, per-screen status, scroll controls
3. Per-monitor targeting — different content on each monitor, confirmed
4. Scroll — nudge, jump, auto-scroll, keyboard; works inside a PDF

**On `main` (`312e750`), committed but not yet deployed: agent-owned display
power.**
The Pi has no keyboard, so anything that blanks the screen and wakes only on
*input* can only be cured by unplugging it — which is what was happening. The
agent now claims DPMS at startup (timeouts zeroed, DPMS kept enabled) and drives
power itself: idle on its home page → off after 10 min, showing a site → off
after 2 h without a request, and **any `/v1` call wakes it**. `POST /v1/display`
and a "Display off" button cover leaving the room. Both monitors sleep together
(X11 has no per-output power). Same work made screens self-detecting from
`xrandr --listmonitors`, so a fresh install needs no `[[screen]]` blocks typed by
hand. See `agent/display.py`, `deploy/pi/README.md` §8.

**New this session, uncommitted: the Windows tray app.**
`deploy/windows/roomtray.ps1` — copy a link or file, double-click the tray icon,
it's on the wall. The icon colour *is* the status (blue awake, grey asleep, red
unreachable), the tooltip is what the selected screen is showing, and it polls
`/v1/status` every 30 s. Right-click gives screen, Send file, Home, Reload,
Display off / Wake, Open controller, Quit.

It is deliberately **not** Python. A `NotifyIcon` off `System.Windows.Forms`
needs no install, no interpreter and no dependency, so the script plus
`targets.toml` is the whole thing on any desktop on the tailnet — including one
with no checkout. It reads the same `roomctl/targets.toml` (`ROOMCTL_TARGETS`
and `-Target <name>` both honoured) and remembers the chosen screen in
`%APPDATA%\roomtray\screen.txt`.

The web UI is untouched and keeps everything else — scroll controls, saved
links, drag-and-drop. "Open controller" is one click away in the menu. The tray
covers the verbs you hit twenty times a day without opening a tab.

`deploy/windows/selfcheck.ps1` runs the offline half: TOML parse, the 63-char
tooltip clamp, the icon, error-detail extraction. It pulls the real script's
functions out of its AST rather than dot-sourcing, because dot-sourcing would
start the tray and block. See `deploy/windows/README.md`.

**New this session, uncommitted: the screens editor — PLAN.md §7's v1.1.0.**
The controller page is now the home base. The agent token goes into a real field
in a **Settings** panel instead of a `prompt()`, and a 401 reopens that panel and
says the token was rejected rather than silently clearing it. The same panel
edits each screen's **name, home URL, position and size**, applied live through
the existing `browser.place()` — the window moves while you watch, which is the
only reason this beats editing a file over `ssh`.

Overrides persist to `~/.local/share/room-display/settings.json` (`ROOM_SETTINGS`
overrides the path), a file the agent owns and `update.sh` never replaces.
**JSON, not the `screens.toml` PLAN.md called for** — `tomllib` only reads, and a
TOML writer is a new dependency for a file no human edits.
`/etc/room-display/config.toml` stays `root:<user> 640` and un-writable by the
agent, exactly as planned, so the token never becomes agent-editable.

Three things are load-bearing and easy to undo by accident:

- **Overrides match screens by index, not name**, because the name is itself
  editable — matching on it would make every rename look like a new screen.
- **`?screen=` is re-stamped, not appended.** It used to append only when
  absent, so a rename left the old name on the idle page forever.
- **`load_config()`'s result is merged into the live cfg with `clear()` +
  `update()`, never reassigned.** `display.watch()` closed over that dict at
  startup; reassigning would leave the idle watcher on a stale config.

Display sleep timeouts and upload caps were considered and deliberately left
file-only. `All` in the screen picker is now capitalised — label only, the wire
value is still `"all"`.

## The next thing to do

**v1.0.1 is tagged, pushed and live — that part is done.** Checked against the
Pi on 2026-08-28: `/v1/status` returns `"version": "v1.0.1"`, screens `Samsung`
and `Acer`, `browser: ok`. Display power is deployed. What is left of Phase 8 is
one item:

1. **The rollback acceptance.** Tag a deliberately broken `v1.0.2` and confirm it
   either refuses at selfcheck or rolls back to v1.0.1. Nothing has ever
   exercised that branch, and it is the only thing standing between a bad tag and
   a Pi nobody can fix without a keyboard.

Still worth doing while you are on the box, if it hasn't been: confirm
`DISPLAY=:0 xset q | grep -A3 '^DPMS'` shows **Enabled** with timeouts
**0 0 0** under the deployed agent, rather than only from the hand-run probe.

**Separately, and independent of the Pi work: look at the tray icon.** It starts
without crashing against the real Pi, but nobody has seen it. Check the icon
appears and reads at 16x16 on the taskbar, that the menu opens and lists Samsung
/ Acer / all, that double-click sends the clipboard, and that blue/grey/red are
distinguishable there. Then commit `deploy/windows/` — it touches no Python, so
it can go in on its own.

    powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File "e:\Company\Github\CrossDrop\deploy\windows\roomtray.ps1"

Quit from the tray menu. It reads `roomctl/targets.toml` via `$PSScriptRoot`, so
it can be launched from any working directory.

**And: run the v1.1.0 acceptance.** The screens editor is covered by 22 tests but
has never touched the Pi. Deploy it, then from the page: rename `Acer`, press
Home on it, and confirm the idle screen announces the new name (that proves the
`?screen=` re-stamp). Swap the two positions, save, and watch the windows change
monitors with no restart — that is PLAN.md §7's stated acceptance. Put them back,
then blank a position and confirm it returns to the detected value. Restart the
agent and confirm the surviving overrides come back.


## The display Pi's actual state

Host `STUDYPERIPHERAL`, user `admin`, Trixie, **X11** — Xorg `:0` under lightdm
on vt7, window manager openbox (`rpd-rc.xml`). No labwc or wayfire running.
(An earlier note here said labwc; that was wrong. Confirmed 2026-08-27 by
`pgrep -a Xorg` on the box. `wlopm`/`wlr-randr` are installed but inert on X11.)

    HDMI-1  Samsung  1366x768   position 0,0      screen name "samsung"
    HDMI-2  Acer     2560x1440  position 1366,0   screen name "acer"

Connector names are `HDMI-1`/`HDMI-2` under X11 (`xrandr --listmonitors`), not
the kernel's `HDMI-A-1`/`HDMI-A-2` that `/sys/class/drm` reports.

Now on the **Phase 8 release layout**, migrated by the v1.0.0 deploy:

    /opt/room-display/current -> releases/v1.0.0     (VERSION=ROOM_VERSION=v1.0.0)
    /opt/room-display/cache-repo/                    bare clone, fetched --tags
    /opt/room-display/pre-phase8-20260827193125/     the old Phase 5 clone, kept

Releases are `git archive` exports, so **they contain no `.git` and `git pull` on
the Pi fails** — that is by design, not breakage. Updates come from tags only.

Units installed: `display-agent.service`, the snapshot units (timer disabled) and
the update units. `room-display-update.timer` is **enabled and active** — the Pi
now deploys new tags on its own within ~30 min, so a pushed tag is a deploy.

Display power was proved on this box by hand: `xset dpms force off` → `Monitor is
Off` → `force on` → `Monitor is On`, no keyboard. `display.claim()` and
`display.detect()` were run against the real X session from a temp copy —
detect() returns `HDMI-1 0,0 1366x768` and `HDMI-2 1366,0 2560x1440`, claim()
leaves DPMS Enabled at `0 0 0` with the screensaver off. Those settings live with
the X session, so they last until reboot; after the v1.0.1 deploy the agent
re-applies them at every start.

The Samsung runs at 1366x768 (its EDID preferred) though it supports 1080p.
Bumping it changes the Acer's offset to `1920,0` and needs the mode persisting
across reboots — deliberately not done.

## Gotchas found the hard way (all fixed, don't re-derive)

- **The compositor may apply the window move asynchronously.** Moving then
  immediately fullscreening puts the window back on its old monitor. Fixed with a
  300 ms settle in `browser._place`, tunable via `ROOM_PLACE_SETTLE`.
- **X11 has no per-output display power.** `xrandr --prop` reports no DPMS
  property on either output, so `xset dpms force off` takes both monitors or
  neither. The per-output alternative (`xrandr --output X --off`) drops the CRTC
  and reflows the layout, which moves the kiosk windows.
- **`raspi-config`'s screen-blanking setting disables DPMS on X11**, which would
  make `xset dpms force off` a silent no-op. `display.claim()` re-enables it and
  zeroes the timeouts instead.
- **Window 1 must be moved, not opened.** `--kiosk` already placed it; its
  `position` was silently ignored until `place()` was added.
- **The wheel event must hit the viewport centre.** A fixed point near the
  top-left lands in Chromium's PDF thumbnail sidebar and scrolls that instead.
- **Jumps use one oversized wheel delta**, not Home/End — key events do not
  reliably reach the PDF viewer's embedded frame. Scroll offsets clamp, so it
  lands exactly at the end.
- **`?screen=<name>` is appended to each screen's `home_url`** at config load.
  Without it every monitor renders the first screen's name.
- **Trixie ships Debian's `chromium`** — `/usr/bin/chromium`, no
  `chromium-browser`. Verified against Debian's package file list.
- **`DISPLAY=:0` is required** for anything X11 run over SSH, including the
  placement probe.
- **`display.claim()` runs on a background thread, not in `lifespan`.** Lifespan
  blocks the port from binding, and `update.sh` rolls a release back if
  `/v1/status` does not answer within 30 s of the restart — five `xset` calls
  have no business inside that window.
- **The X session's DPMS settings do not survive a reboot**, which is why the
  agent claims them at every start rather than `setup.sh` doing it once.

PowerShell 5.1, from building the tray app — all three cost real time:

- **`$_.Exception.Response.GetResponseStream()` reads empty after
  `Invoke-RestMethod`.** It has already drained the stream, so every agent error
  message came back as `""`. `$_.ErrorDetails.Message` holds the body; use that.
- **`Invoke-RestMethod -Form` is PowerShell 7+.** Uploads go through
  `System.Net.Http.MultipartFormDataContent` instead — hand-rolling multipart
  boundaries around binary bytes in 5.1 is how you corrupt a PDF.
- **FastAPI's 422 `detail` is a list of objects, not a string**, and
  `ConvertFrom-Json` unrolls a one-element list to the object — so test for
  `.msg`, not for `[array]`, or a bad URL prints a hashtable dump at the user.

## Open decisions

- **Config editing in the desktop UI.** Deferred, and now written up as
  **v1.1.0 in PLAN.md §7** with its constraints. Narrow slice only: a screens
  editor with live apply. Token and install-time paths stay file-only.
- **Saved links are per-browser** (`localStorage`), so the desktop and laptop
  each keep their own. Moving them to the agent was offered and not taken.
- **Repo is public.** PLAN.md §10 assumes private + read-only deploy key. No
  secrets are exposed (configs ship as `.example`, tokens git-ignored) and
  `update.sh` needs no credential while public — but §12 item 4 is only closed
  *because* of that choice. Going private again means adding a deploy key.
- **Firefox has no multi-screen or scroll** (CDP-only). Returns 501. Use
  `kind = "edge"` to test multi-screen on Windows.
- **A branch-based deploy was considered and dropped.** `update.sh` picks the
  highest `v*` tag in the repo and cannot see branches at all, so an `unstable`
  branch protects nothing by itself — what protects the Pi is only tagging what
  you have decided is good. Making the Pi able to *run* a branch is a real
  change to the one script with no keyboard behind it, and would need its own
  rollback testing.
- **The tray app deliberately stops at the frequent verbs.** No global hotkey
  (needs `RegisterHotKey` plus a message pump; add it if double-click stops
  feeling close enough), no clipboard *images* (would mean writing a temp PNG for
  a case the file picker covers), no scroll or saved links — those stay in the
  web UI, which the menu can open. The icon is drawn at runtime rather than
  shipping a `.ico`, because the colour is the whole feature.
- **Per-screen `output` was deliberately not stored** on each screen. X11 powers
  every monitor together, so a connector name per screen would be dead data; it
  is one line to add if per-monitor power ever lands (PLAN.md §7 future
  considerations).

## Not verified by anyone

- **The rollback branch of `update.sh`** — deploy-forward now works on hardware,
  but nothing has ever failed a selfcheck or a post-restart health check for
  real. See "the next thing to do", item 4.
- **The wake half of display power.** v1.0.1 is deployed and the Pi reported
  `"awake": false` on 2026-08-28, so the monitors do go dark under the running
  agent — though that could equally have been a "Display off" click, the two are
  indistinguishable from `/v1/status`. What is still unproven is the *wake*: a
  pushed link turning a dark display back on. One double-click of a copied link
  in the tray app settles it.
- **The tray app on screen.** Every non-GUI part was run for real against a live
  agent booted on `127.0.0.1:8099` (`ROOM_CONFIG` pointed at a temp config with
  `autolaunch = false`): TOML parse, tooltip clamp, icon draw, error detail on
  401/404/415/422/503, the stale-screen re-pinning in `Poll`, red on unreachable,
  and an upload of 256 raw bytes that landed **SHA256-identical** in the agent's
  upload dir. What nobody has seen is the tray icon itself — that it appears,
  that the menu opens, that double-click sends the clipboard, that the colours
  read at 16x16 on the taskbar. Run it and look; that is the only thing standing
  between this and committing `deploy/windows/`.
- **The screens editor against a real browser.** All 22 tests run without one, so
  what they prove is the file, the validation and the routing — that a bad
  position never reaches disk, that a rename re-stamps `?screen=`, that a dead
  browser returns a `note` instead of a 500. What no test can prove is
  `browser.place()` actually moving a kiosk window on the Pi when the position
  changes; that path is exercised only by the live half of the acceptance above.
- **The Settings panel on screen.** The page's ids and JS selectors are checked
  both ways by `tests/test_web.py` and the script parses under `node --check`,
  but nobody has opened the panel. Per your standing rule I did not screenshot it.
- Idle SD writes measured ~1.5 MB/10 min, all from tailscaled and
  `.xsession-errors`, none from this project. Judged fine for a 32 GB card
  (~2.5 full-card writes/year) and deliberately not chased further.
- `profile.tar.gz` being mode 0600 — unverifiable on Windows, needs
  `stat -c %a ~/.local/share/room-display/profile.tar.gz` on the Pi.
