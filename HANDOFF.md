# Handoff — 2026-08-26

State at end of session. Everything below is committed and pushed (`8645c7b`),
43 tests passing, ruff clean.

## Where the project is

Phases 1, 3, 4, 5, 6, 8 done. Phase 2 (`roomctl`) done. **Phase 7 (eve) is the
only phase never started**, and `roomctl` already unblocks it.

The four v1.0.0 features are built **and verified on real hardware**:

1. Home screen — both monitors show it, each naming itself
2. Desktop GUI — screen picker, per-screen status, scroll controls
3. Per-monitor targeting — different content on each monitor, confirmed
4. Scroll — nudge, jump, auto-scroll, keyboard; works inside a PDF

## The next thing to do

**Tag v1.0.0 and run the Phase 8 acceptance on the spare Pi.** This is the only
mechanism in the project that has never run outside stubs, and it is the safety
net for every future change. Do not enable auto-update on the display Pi until
the spare has survived it.

Order:

1. Check CI is green on GitHub (it has run on pushes; confirm before tagging).
2. On the **spare** Pi (`rpi4b`): install units, then
   `systemctl --user enable --now room-display-update.timer`.
3. `git tag v1.0.0 && git push --tags` → within 30 min the spare deploys it and
   `/v1/status` reports `"version": "v1.0.0"`.
4. Deliberately break something, tag `v1.0.1`, and confirm the spare either
   refuses at selfcheck or rolls back. **That is the acceptance test.**
5. Only then enable the timer on the display Pi.

`systemctl --user start room-display-update` forces a check without waiting.

## The display Pi's actual state

Host `STUDYPERIPHERAL`, user `admin`, Trixie, labwc (Wayland) — **not** switched
to X11; it turned out not to be necessary.

    HDMI-A-1  Samsung  1366x768   position 0,0      screen name "samsung"
    HDMI-A-2  Acer     2560x1440  position 1366,0   screen name "acer"

Still on the **Phase 5 layout** — `/opt/room-display/current` is a plain git
clone, not a symlink. Updates have been manual (`git pull` + restart). The first
tag deploy will migrate it to the release layout; that migration path is tested
in WSL but not on hardware.

Units installed on it: `display-agent.service` plus the snapshot and update
units, all timers **disabled**.

The Samsung runs at 1366x768 (its EDID preferred) though it supports 1080p.
Bumping it changes the Acer's offset to `1920,0` and needs the mode persisting
across reboots — deliberately not done.

## Gotchas found the hard way (all fixed, don't re-derive)

- **labwc applies the window move asynchronously.** Moving then immediately
  fullscreening puts the window back on its old monitor. Fixed with a 300 ms
  settle in `browser._place`, tunable via `ROOM_PLACE_SETTLE`. This is why the
  X11 switch was avoided — labwc does honour the move, just not instantly.
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

## Not verified by anyone

- The tag → deploy → rollback path on real hardware (see "next thing to do").
- Idle SD writes measured ~1.5 MB/10 min, all from tailscaled and
  `.xsession-errors`, none from this project. Judged fine for a 32 GB card
  (~2.5 full-card writes/year) and deliberately not chased further.
- `profile.tar.gz` being mode 0600 — unverifiable on Windows, needs
  `stat -c %a ~/.local/share/room-display/profile.tar.gz` on the Pi.
