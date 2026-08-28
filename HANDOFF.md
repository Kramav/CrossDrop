# Handoff — 2026-08-28

State at end of session. `main` is at `312e750`; 49 tests passing + 2 skipped,
`ruff check --select E9,F` clean (what CI runs).

**Uncommitted in the working tree** — nothing here is pushed yet:

    ?? deploy/windows/        the tray app, its README, its selfcheck
     M README.md              tray app added to "Controlling a display"
     M HANDOFF.md             this file, including last session's rewrite

No Python changed this session, so the test suite and CI are unaffected by it —
the 49/2 above is the same number `312e750` produced.

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

## The next thing to do

**Tag v1.0.1 to ship display power, then finish the Phase 8 acceptance.**

1. Confirm CI is green on `312e750` before tagging — that gate is the entire
   point of the pipeline.
2. `git tag v1.0.1 && git push --tags`. The update timer is **enabled and
   active**, so it lands within ~30 min on its own; force it with
   `systemctl --user start room-display-update` and watch
   `journalctl --user -u room-display-update -f`. Worth watching live: the v1.0.0
   deploy was run by hand, so **the unit itself has never run** (it has no
   journal entries at all).
3. Verify on the Pi: `/v1/status` reports `"version": "v1.0.1"` and
   `"awake": true`; `DISPLAY=:0 xset q | grep -A3 '^DPMS'` shows **Enabled** with
   timeouts **0 0 0**. Then drop `idle_off_minutes = 1` / `content_off_minutes = 2`
   into `/etc/room-display/config.toml`, restart, send both screens home, wait for
   them to go dark, and push a link — they must wake. Remove the block after.
4. **Still outstanding: the rollback acceptance.** Tag a deliberately broken
   `v1.0.2` and confirm it either refuses at selfcheck or rolls back to v1.0.1.
   Nothing has ever exercised that branch, and it is the only thing standing
   between a bad tag and a Pi nobody can fix without a keyboard.

**Separately, and independent of the Pi work: look at the tray icon.** It has
never been on screen. Run it, check the icon appears and reads at 16x16 on the
taskbar, that the menu opens, that double-click sends the clipboard, and that
blue/grey/red are distinguishable there. Then commit `deploy/windows/` — it
touches no Python, so it can go in on its own without waiting for the tag.

    powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File deploy\windows\roomtray.ps1


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
- **Display power end to end under the running agent.** The mechanism, the
  parser and `claim()` were each proved on the Pi by hand, and the policy is
  covered by `tests/test_display.py` with a faked clock — but no monitor has yet
  gone dark on an idle timer and woken from a pushed link. That needs the v1.0.1
  deploy.
- **The tray app on screen.** Every non-GUI part was run for real against a live
  agent booted on `127.0.0.1:8099` (`ROOM_CONFIG` pointed at a temp config with
  `autolaunch = false`): TOML parse, tooltip clamp, icon draw, error detail on
  401/404/415/422/503, the stale-screen re-pinning in `Poll`, red on unreachable,
  and an upload of 256 raw bytes that landed **SHA256-identical** in the agent's
  upload dir. What nobody has seen is the tray icon itself — that it appears,
  that the menu opens, that double-click sends the clipboard, that the colours
  read at 16x16 on the taskbar. Run it and look; that is the only thing standing
  between this and committing `deploy/windows/`.
- Idle SD writes measured ~1.5 MB/10 min, all from tailscaled and
  `.xsession-errors`, none from this project. Judged fine for a 32 GB card
  (~2.5 full-card writes/year) and deliberately not chased further.
- `profile.tar.gz` being mode 0600 — unverifiable on Windows, needs
  `stat -c %a ~/.local/share/room-display/profile.tar.gz` on the Pi.
