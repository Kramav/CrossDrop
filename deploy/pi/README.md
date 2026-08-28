# Pi provisioning (Phases 5–6)

Gets the agent running on the Pi under the autologin desktop session, with the
browser profile in RAM and snapshotted to SD so logins survive a reboot.

Paths match PLAN.md §8, so Phase 8 only has to repoint the `current` symlink —
nothing here changes when auto-update lands.

    /opt/room-display/current               code + venv   (a plain dir now, a symlink in Phase 8)
    /etc/room-display/config.toml           token, paths  (never overwritten by updates)
    /run/user/1000/room-display/profile     browser profile — tmpfs, so RAM (Phase 6)
    /run/user/1000/room-display/uploads     uploads — tmpfs too
    ~/.local/share/room-display/profile.tar.gz   the only thing that touches SD (0600)

**Just want it running?** [setup.sh](setup.sh) does this page *and* pi-setup.md
§2-§7 in one go, on a card you've already imaged:

```sh
curl -fsSL https://raw.githubusercontent.com/Kramav/CrossDrop/main/deploy/pi/setup.sh | bash
```

The rest of this page is what it does, for when a step needs debugging.

## 1. A configured Pi

[pi-setup.md](pi-setup.md) first — imaging, SSH, Tailscale, desktop autologin,
screen blanking off, display left on auto-detect. Its step 7 is the checklist
that says you're ready for this page.

## 2. Packages

```sh
sudo apt update
sudo apt install -y python3-venv git
sudo apt install -y chromium || sudo apt install -y chromium-browser
```

Trixie Pi OS ships Debian's `chromium` package, which installs `/usr/bin/chromium`
and **no** `/usr/bin/chromium-browser`. Bookworm and earlier shipped Raspberry
Pi's own `chromium-browser` build. `browser.py` and `setup.sh` both accept either
name, so don't "fix" a script that calls the one you don't have.

Tailscale should already be up (`tailscale ip -4` prints a 100.x address).

## 3. Code

```sh
sudo mkdir -p /opt/room-display
sudo chown "$USER" /opt/room-display
git clone <repo> /opt/room-display/current
cd /opt/room-display/current
python3 -m venv .venv
.venv/bin/pip install -r agent/requirements.txt
```

## 4. Config

```sh
sudo mkdir -p /etc/room-display
sudo cp /opt/room-display/current/agent/config.example.toml /etc/room-display/config.toml
sudo nano /etc/room-display/config.toml
```

`setup.sh` writes this file for you, including a random token — the manual route
below is for a hand-built Pi. Monitors are detected at startup and display
timeouts default in code, so neither needs a line here.

Pi values — the rest of the file is fine as shipped:

```toml
token = "<a long random string>"
home_url = "about:blank"

[browser]
kind = "chromium"
profile_dir = "/home/<user>/.local/share/room-display/profile"

[upload]
dir = "/run/user/1000/room-display/uploads"
```

`/run/user/1000` is tmpfs, so uploads are in RAM with no setup — that is the
uploads half of Phase 6 already done. `max_mb` × `keep` is the RAM ceiling:
the shipped 25 × 20 can reach 500 MB, so lower `keep` on a 2 GB Pi.

The token is a secret, and the file is world-readable by default:

```sh
sudo chown root:"$USER" /etc/room-display/config.toml
sudo chmod 640 /etc/room-display/config.toml
```

## 5. Service

```sh
mkdir -p ~/.config/systemd/user
cp /opt/room-display/current/deploy/pi/display-agent.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now display-agent
```

No `kiosk-launch.sh`: the agent launches and owns the kiosk itself, so a
separate launcher would just be a second thing to keep in sync.

## 6. RAM profile and snapshots (Phase 6)

`/run/user/1000` is already tmpfs, so pointing `profile_dir` at it is the whole
of "profile in RAM" — no fstab entry, no mount unit. What that costs is that the
profile is empty every boot, which is what [profile-snapshot.sh](profile-snapshot.sh)
exists to fix:

```sh
profile-snapshot.sh restore   # SD -> RAM, ExecStartPre
profile-snapshot.sh save      # RAM -> SD, ExecStopPost
```

It archives the **whole profile minus caches**, not just `Cookies` — Local
Storage and IndexedDB hold session tokens too, and an allowlist would quietly
log you out of exactly the pages you cared about. The archive is `0600`.

Two guards worth knowing about, because both look like bugs otherwise:

- **A save inside 5 minutes of the last one is skipped.** `Restart=always` fires
  `ExecStopPost` on every crash-loop iteration; without the floor that's an SD
  write every 5 seconds, which is the wear this phase exists to prevent.
- **A corrupt snapshot never blocks startup.** It's moved to `.bad` and the Pi
  comes up with a fresh profile. You lose the logins, not the display.

Default is snapshot-on-stop (PLAN.md §9 option (a)). If the study loses power
often, take option (b) — the timer is already installed, just disabled:

```sh
systemctl --user enable --now room-display-snapshot.timer
```

Logs go to RAM too, via [journald-volatile.conf](journald-volatile.conf). Read
that file before enabling it: the trade is that the journal doesn't survive a
reboot, which is exactly when you most want it.

Cache size matters now that the profile is RAM: `disk_cache_mb` (default 100)
caps it. Uncapped, Chromium sizes its cache from free space and fills
`/run/user/1000`. Watch the headroom with `df -h /run/user/1000` — uploads share
it, and the shipped `max_mb` × `keep` can reach 500 MB on its own.

## 7. Two monitors

**The agent detects them itself.** With no `[[screen]]` blocks in the config it
reads `xrandr --listmonitors` and makes one screen per connected output, named
after the connector. Configure blocks below only to override that — to give the
screens better names than `HDMI-1`, or to give one its own `home_url`.

The study Pi runs an **X11 session** (Xorg + openbox under lightdm), where
Chromium's `--window-position` is honoured directly. Check what you're on:

```sh
pgrep -a Xorg && echo "X11"          # openbox/lightdm: positions just work
pgrep -a labwc && echo "wayland"     # then read the note below
```

Under Wayland the compositor has final authority over window position and
`--window-position` is ignored, so per-monitor targeting depends on Chromium
running under XWayland (`--ozone-platform=x11`, which `browser.py` adds for
multi-screen) and the compositor honouring the X11 move request. If windows land
on the wrong monitor there, the probe is:

```sh
wlr-randr        # read the second output's "Position:" line — that x is what you want
# DISPLAY=:0 is required: over SSH it is unset, and Chromium's X11 backend then
# dies with "Missing X server or $DISPLAY" without telling you anything about
# whether the compositor would have honoured the move.
DISPLAY=:0 chromium --ozone-platform=x11 --user-data-dir=/tmp/probe \
         --window-position=<x from wlr-randr>,0 --window-size=800,600 about:blank &
```

Take the offset from the compositor, not from the monitor's resolution — outputs
are laid out edge to edge, so a 1366-wide first screen puts the second at
`1366,0`.

Note that display power (§8) is X11-only: on Wayland the agent leaves the
monitors alone.

Then in `/etc/room-display/config.toml`:

```toml
[[screen]]
name = "left"
position = "0,0"
[[screen]]
name = "right"
position = "1920,0"
```

One browser, one profile, one window per screen — so both monitors share your
logins and Phase 6 still snapshots a single profile. Two browser instances would
double the tmpfs footprint and split your cookies in half.

The **first** screen lands wherever the compositor puts the kiosk window;
`position` only steers the second and later ones. Order the config so the screen
you care least about is first.

```sh
roomctl screens
roomctl navigate https://example.com --screen right
roomctl upload notes.pdf --screen left
roomctl navigate https://example.com --screen all
```

Omit `--screen` and you hit the first one, which is exactly what a single-monitor
Pi does today.

## 8. Display power

The Pi has no keyboard, so a screen that blanks on a timer and wakes on input is
a trap — the only cure left is unplugging the box. The agent therefore owns
display power: at startup it zeroes every automatic timeout (`xset dpms 0 0 0`,
`xset s off`) while keeping DPMS **enabled**, then drives power itself.

- Idle on its home page for `idle_off_minutes` (10) → monitors off.
- Showing a site: `content_off_minutes` (120) with no API activity → off.
- **Anything you send wakes them.** Navigate, upload, scroll, home, reload — a
  dark display is never a stuck display.
- `POST /v1/display {"action":"off"}`, or the "Display off" button in the web UI,
  for leaving the room.

Both monitors sleep together: X11 has no per-output power (`xrandr --prop`
reports no DPMS property on either output). One screen showing something keeps
the pair awake.

Check it took:

```sh
DISPLAY=:0 xset q | grep -A3 "^DPMS"     # Enabled, and 0 0 0 — the agent's doing
```

If that shows non-zero timeouts, the agent is not managing power (wrong session,
no `DISPLAY`, or no `xset`) and the OS will blank the screen on its own. The
journal says which: `journalctl --user -u display-agent | grep display:`.

Overrides, if the defaults don't suit — `0` disables that timer:

```toml
[display]
idle_off_minutes = 10
content_off_minutes = 120
```

## 9. Auto-update (Phase 8)

The Pi pulls; GitHub never reaches in. Every ~30 min
[update.sh](update.sh) asks GitHub for the highest `v*` tag and does nothing at
all if it already runs it — no clone, no writes, which is what keeps a timer
firing 48×/day off the SD card.

When there *is* a new tag:

1. `git archive` it into `releases/<tag>/` (no per-release `.git`)
2. build a per-release venv
3. **boot check** — `python -m agent selfcheck` from the new venv, in-process,
   no port, no browser. Fails → the swap never happens.
4. swap the `current` symlink, restart the agent
5. **verify the live port** for 30 s — this is what catches runtime and kiosk
   regressions a boot check cannot see
6. not healthy → **roll back** to the previous release and restart
7. prune to the last 3, never the running or previous one

Enable it when you're ready to let the Pi replace its own code:

```sh
systemctl --user enable --now room-display-update.timer
systemctl --user list-timers room-display-update.timer
```

**Cutting a release.** Push to main, wait for CI green, then tag:

```sh
git tag v1.0.0 && git push --tags
```

Within 30 minutes the Pi is on it, and `/v1/status` reports `"version": "v1.0.0"`
— that comes from a `VERSION` file update.sh writes into each release dir and
the unit reads via `EnvironmentFile`.

**Layout.** Updates swap code only. Your token, snapshot and uploads live
outside the release tree and are never touched:

    /opt/room-display/cache-repo/      one bare clone, fetched --tags
    /opt/room-display/releases/<tag>/  code + venv, one per release
    /opt/room-display/current -> releases/<tag>

**Test the rollback before you trust it** — that's PLAN.md §7's acceptance, and
it's the only feature here that matters. Use the spare Pi: tag a deliberately
broken commit, watch it refuse to deploy or roll itself back, then tag a good
one and watch it land.

```sh
journalctl --user -u room-display-update -f
systemctl --user start room-display-update    # don't wait for the timer
```

A failed update leaves the unit failed on purpose (`systemctl --user
list-units --failed`) while the display keeps running the old release.

**Credentials:** none, while the repo is public — `git ls-remote` and
`git archive` work unauthenticated. If you take it private, put an SSH url in
`REPO` and a **read-only deploy key** on the Pi (PLAN.md §8), never a personal
token.

## 10. Acceptance (PLAN.md §7 Phase 5)

```sh
sudo reboot
```

Desktop autologins, kiosk comes up, and from the Win10 desktop or Win11 laptop —
nobody at the Pi:

```sh
curl -H "Authorization: Bearer <token>" http://<pi-tailnet-ip>:8080/v1/status
curl -X POST -H "Authorization: Bearer <token>" -H 'Content-Type: application/json' \
     -d '{"url":"https://example.com"}' http://<pi-tailnet-ip>:8080/v1/navigate
```

The screen should change. Then open `http://<pi-tailnet-ip>:8080/` in an app
window for the drop-zone UI:

```
msedge.exe --app=http://<pi-tailnet-ip>:8080/
```

## Troubleshooting

`journalctl --user -u display-agent -f` is the log.

- **Restart loop, "debug port 9222 never came up"** — the compositor was not up
  yet; systemd retries every 5 s, so give it a minute after boot. If it never
  settles, the unit's `DISPLAY`/`WAYLAND_DISPLAY` don't match the session —
  read the real values with the recipe in [pi-setup.md](pi-setup.md) §6.
- **`tailscale ip -4` fails in the unit** — your user cannot query tailscaled.
  Put the literal 100.x address in `ExecStart` instead.
- **Agent starts but the screen never changes** — something else already holds
  9222 and the agent is driving *that* browser. `pgrep -a chromium`; kill the
  strays and restart. (This is why PLAN.md §6 wants the profile dir dedicated.)
- **Dropped files 404 on the display** — the browser is fetching a hostname the
  Pi itself cannot resolve. Reach the agent by tailnet IP or MagicDNS name.
- **"Unlock your login keyring" dialog over the kiosk** — Chromium's default
  password store on Linux is the system keyring, and desktop autologin never
  unlocks it, so it asks. The agent launches with `--password-store=basic` to
  avoid it entirely. If you see this, you're on older code: `git pull` in
  `/opt/room-display/current` and `systemctl --user restart display-agent`.
  If a dialog somehow survives, `pkill -u "$USER" chromium` and restart — the
  agent relaunches its own browser.
- **Blank white screen** — that's `home_url = "about:blank"` rendering, i.e. the
  kiosk working. Prove it with `/v1/navigate`; set `home_url` in
  `/etc/room-display/config.toml` if you want something else at boot.
