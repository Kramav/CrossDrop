#!/usr/bin/env bash
# Provisions a display box from a fresh install to a running agent. Two kinds:
# a Raspberry Pi (pi-setup.md §2-§7 and README.md §2-§5), or a plain Debian box
# with monitors on it — a Proxmox host is the case this was written for, see
# deploy/linux.md. Re-runnable.
#
#   curl -fsSL https://raw.githubusercontent.com/Kramav/CrossDrop/main/deploy/pi/setup.sh | bash
#
# To start over instead of re-running: deploy/pi/uninstall.sh.
#
# The two differ in exactly two places: the Pi has a graphical session already
# and needs raspi-config plus a boot-config edit; the Debian box has no session
# at all and needs one built. Everything after "== code" is identical.
#
# NOT covered — they need a human or a Windows box:
#   - imaging the card with Pi Imager (hostname, wifi, SSH key)  [pi-setup.md §1]
#   - authenticating Tailscale (prints a URL, you open it)
#   - disabling Tailscale key expiry in the admin console
set -euo pipefail

REPO="${REPO:-https://github.com/Kramav/CrossDrop.git}"
VIDEO="${VIDEO:-}"   # empty = auto-detect (default). Override only for a Pi that
                     # boots with no monitor attached: VIDEO=HDMI-A-1:1920x1080@60D
PORT="${PORT:-8080}"

# PREFIX exists so tests/test_install_roundtrip.py can run this script for real
# against a temp tree. Empty in production, and every path below is then the
# absolute one it always was. Do not add a path here that is not derived from it.
PREFIX="${PREFIX:-}"
OPT="${OPT:-$PREFIX/opt/crossdrop}"
ETC="${ETC:-$PREFIX/etc/crossdrop}"
CFG="${CFG:-$ETC/config.toml}"
RUN="${RUN:-$PREFIX/run/user/$(id -u)/crossdrop}"
SYSTEMD_SYS="${SYSTEMD_SYS:-$PREFIX/etc/systemd/system}"
JOURNALD_D="${JOURNALD_D:-$PREFIX/etc/systemd/journald.conf.d}"
CMDLINE="${CMDLINE:-$PREFIX/boot/firmware/cmdline.txt}"
DRM="${DRM:-$PREFIX/sys/class/drm}"
UNITS="${UNITS:-$HOME/.config/systemd/user}"
DATA="${CROSSDROP_DATA:-$HOME/.local/share/crossdrop}"

# A v1 box (room-display / display-agent) must migrate, not install alongside.
# Two trees means two agents racing for :8080 and two autostart units, and the
# one that wins is whichever systemd started first -- on a box with no keyboard.
# MIGRATE=1 does it inline, for the one-command case.
OLD_OPT="${OLD_OPT:-$PREFIX/opt/room-display}"
# Note the missing `&& [ ! -d "$OPT" ]`. That guard failed open on exactly the
# state it most needed to catch: migrate.sh creates $OPT (to move the extensions
# into) before it deletes $OLD_OPT, so an interrupted migration has *both*. A
# re-run then installed alongside, left both unit sets enabled, and minted a
# second bearer token while the live one was still in the old config -- verbatim
# the outcome this refusal exists to prevent. The presence of $OLD_OPT is the
# whole condition; a half-migrated box needs migrate.sh more, not less.
if [ -d "$OLD_OPT" ]; then
  # migrate.sh ships in v2 and no v1 tag contains it, so it is NOT on a v1 box:
  # $OLD_OPT/current is a v1 checkout. And $0 is "bash" under `curl ... | bash`,
  # which is the only install method documented. So the only command we can
  # print that actually works is one that fetches it.
  MIGRATE_URL="${MIGRATE_URL:-https://raw.githubusercontent.com/Kramav/CrossDrop/main/deploy/pi/migrate.sh}"
  HERE=""
  case "$0" in
    */*) if [ -f "$(dirname "$0")/migrate.sh" ]; then HERE="$(dirname "$0")"; fi ;;
  esac
  if [ "${MIGRATE:-0}" = 1 ]; then
    echo "== migrating the v1 install first"
    if [ -n "$HERE" ]; then
      exec bash "$HERE/migrate.sh"
    fi
    # Piped, or run from a v1 checkout that predates migrate.sh: fetch it.
    TMP_MIG="$(mktemp)"
    if curl -fsSL "$MIGRATE_URL" -o "$TMP_MIG" && [ -s "$TMP_MIG" ]; then
      # `exec` never returns, so the copy cannot be cleaned up afterwards --
      # hand it to migrate.sh's own EXIT trap instead.
      exec env CROSSDROP_MIGRATE_TMP="$TMP_MIG" bash "$TMP_MIG"
    fi
    rm -f "$TMP_MIG"
    echo "MIGRATE=1 but migrate.sh could not be fetched from $MIGRATE_URL" >&2
    exit 1
  fi
  cat >&2 <<EOF
This box has a v1 install at $OLD_OPT, which used the name "room-display".
Installing beside it would leave two agents fighting for port $PORT, two
autostart units, and two different bearer tokens.

Migrate it instead -- the token, the logins and the extensions are carried
over. migrate.sh is new in v2, so it is not on this box yet:

  curl -fsSL $MIGRATE_URL | bash

or re-run this installer with MIGRATE=1 to fetch it and do both in one step:

  curl -fsSL https://raw.githubusercontent.com/Kramav/CrossDrop/main/deploy/pi/setup.sh | MIGRATE=1 bash

To start from nothing instead, from the v1 box's own copy:

  bash $OLD_OPT/current/deploy/pi/uninstall.sh
EOF
  exit 1
fi

IS_PI=0
if command -v raspi-config >/dev/null 2>&1; then IS_PI=1; fi

# Argument checks first, before anything is installed or changed. These used to
# live at "== display mode", by which point apt, the Tailscale login and
# `raspi-config do_boot_behaviour B4` had already altered the box -- so a typo
# in VIDEO= left it half-provisioned and exited 1.
if [ -n "$VIDEO" ]; then
  case "$VIDEO" in
    *:*) ;;
    *) echo "VIDEO must be <connector>:<mode>, e.g. HDMI-A-1:1920x1080@60D" >&2
       exit 1 ;;
  esac
  if [ "$IS_PI" = 0 ]; then
    # The write is Pi-only (cmdline.txt is a Pi bootloader file), so accepting
    # VIDEO= here would be the same silent no-op in a different place.
    echo "VIDEO= only applies to a Raspberry Pi (it edits cmdline.txt). On a" >&2
    echo "plain Debian box set the mode in your X config instead." >&2
    exit 1
  fi
  if [ ! -f "$CMDLINE" ]; then
    # Silently doing nothing is the one outcome that matters: VIDEO= is for a
    # Pi that boots with no monitor attached, and that Pi comes up dark.
    echo "VIDEO=$VIDEO was given but $CMDLINE does not exist. On older images" >&2
    echo "it is /boot/cmdline.txt -- pass CMDLINE=/boot/cmdline.txt, or drop" >&2
    echo "VIDEO= to let the kernel auto-detect every connected output." >&2
    exit 1
  fi
fi

# Ask the kernel, don't trust the environment: `su room` without the `-` leaves
# $USER pointing at the previous account, and this name is baked into an
# autologin unit that only fails at the next boot, with no keyboard to fix it.
USER="$(id -un)"

if [ "$(id -u)" -eq 0 ]; then
  cat >&2 <<'EOF'
run as your normal user, not root — the agent runs as you, and Chromium refuses
to start as root. A Proxmox host usually only has root, so make one:

  adduser --gecos "" room && usermod -aG sudo,video,render,input room
  su - room

EOF
  exit 1
fi
sudo -v

echo "== packages"
# Non-fatal on purpose: a Proxmox host without a subscription 401s on the
# enterprise repo, and that must not abort an install whose packages all come
# from Debian main.
sudo apt update || true
# Pi only, and skippable. Upgrading every package on a hypervisor — kernel
# included, under the VMs — is the admin's decision, not a side effect of
# installing a kiosk. On a Pi it is usually right, but it can turn a 3-minute
# install into 30 plus a reboot, which is a surprise worth being able to decline:
#   UPGRADE=0 bash setup.sh
UPGRADE="${UPGRADE:-1}"
if [ "$IS_PI" = 1 ] && [ "$UPGRADE" = 1 ]; then
  echo "   full-upgrade (UPGRADE=0 to skip; this can take a while)"
  sudo apt full-upgrade -y
fi
sudo apt install -y python3-venv git
# Trixie Pi OS ships Debian's `chromium`, which installs /usr/bin/chromium and
# NO /usr/bin/chromium-browser. Bookworm and earlier shipped Raspberry Pi's own
# `chromium-browser` build. Current name first, old name for an older card.
sudo apt install -y chromium || sudo apt install -y chromium-browser
CHROMIUM="$(command -v chromium || command -v chromium-browser || true)"

echo "== tailscale"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
# --ssh makes this box reachable over Tailscale SSH, under your tailnet ACLs and
# not this script's. That is a real decision and it used to be made silently, so
# it is said out loud and can be declined: TSSSH=0 leaves SSH to whatever the
# image was set up with. Said before it happens, because after `tailscale up`
# runs you have already agreed to it.
TSSSH="${TSSSH:-1}"
if ! tailscale ip -4 >/dev/null 2>&1; then
  if [ "$TSSSH" = 1 ]; then
    echo "   enabling Tailscale SSH on this node (TSSSH=0 to skip)."
    echo "   Access is then governed by your tailnet ACLs — check them."
    sudo tailscale up --ssh                  # prints a URL; open it
  else
    sudo tailscale up                        # prints a URL; open it
  fi
fi
# `|| true`, because `set -e` on this pipeline killed the script here with no
# message at all -- after apt had run, before anything was written. The empty
# case is handled at the config step, which is the only place it matters.
TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
if [ -n "$TS_IP" ]; then
  echo "   $TS_IP"
else
  echo "   no tailscale address yet (is this node authenticated?)"
fi

if [ "$IS_PI" = 1 ]; then
echo "== raspi-config"
sudo raspi-config nonint do_boot_behaviour B4   # desktop autologin
# 1 = disable blanking. Belt and braces only: this covers the window between
# login and the agent starting. Once up, the agent claims DPMS itself (zeroed
# timeouts, explicit on/off) so it can wake monitors nothing else can -- see
# agent/display.py and README.md §8. It re-enables DPMS, which this line disables
# on X11, so the two are safe in either order.
sudo raspi-config nonint do_blanking 1
# Compositor left alone. The unit exports both DISPLAY and WAYLAND_DISPLAY, so
# either session works -- but display power is X11-only.

else
echo "== X session"
# The Pi ships a desktop; a server has nothing to draw on. Build the smallest
# session that satisfies the agent: Xorg, and a window manager because placing
# and fullscreening one window per monitor needs one. No display manager and no
# desktop environment -- the login shell on tty1 starts X, which is also why
# this is X11 and not Wayland: display power (agent/display.py) is X11-only, and
# a keyboard-less box that blanks with nothing able to wake it is the exact bug
# this project exists to avoid.
sudo apt install -y xserver-xorg xinit x11-xserver-utils openbox
sudo mkdir -p "$SYSTEMD_SYS/getty@tty1.service.d"
printf '[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin %s --noclear %%I $TERM\n' "$USER" \
  | sudo tee "$SYSTEMD_SYS/getty@tty1.service.d/autologin.conf" >/dev/null
sudo systemctl daemon-reload
[ -f "$HOME/.xinitrc" ] || echo 'exec openbox-session' > "$HOME/.xinitrc"
# bash reads .bash_profile when it exists and .profile only when it does not, so
# appending to the wrong one is a silent no-op.
PROF="$HOME/.bash_profile"; [ -f "$PROF" ] || PROF="$HOME/.profile"
if ! grep -q CrossDrop "$PROF" 2>/dev/null; then
  cat >> "$PROF" <<'EOF'

# CrossDrop kiosk session. -nocursor because there is no mouse to park the
# pointer out of the way. The agent is a systemd *user* unit, so logging in here
# is also what starts it; it may lose the race with X and retry, which is what
# Restart=always in crossdrop-agent.service is for.
[ "$(tty)" = /dev/tty1 ] && [ -z "${DISPLAY:-}" ] && exec startx -- -nocursor
EOF
fi
fi

echo "== display mode"
if [ "$IS_PI" = 1 ] && [ -f "$CMDLINE" ]; then
# ponytail: no pinning by default. The kernel reads EDID and brings every
# connected output up at its own preferred mode, which is the dynamic behaviour
# we want. Pinning one `video=HDMI-A-1:...` forces that output and leaves the
# second monitor dark — pin only on a Pi that boots with nothing plugged in.
if [ -n "$VIDEO" ]; then
  sudo sed -i -e '1s| video=[^ ]*||g' -e "1s|\$| video=$VIDEO|" "$CMDLINE"
  # Leave a marker so uninstall.sh can tell *our* pin from one the admin set
  # before ever hearing of this project. Without it the uninstaller strips any
  # video= it finds, and a pin removed at random is a Pi that boots dark.
  sudo mkdir -p "$ETC"
  echo "$VIDEO" | sudo tee "$ETC/.video-pin" >/dev/null
  echo "   pinned: $VIDEO"
elif grep -q 'video=' "$CMDLINE"; then
  sudo sed -i '1s| video=[^ ]*||g' "$CMDLINE"              # single line, edit in place
  sudo rm -f "$ETC/.video-pin"
  echo "   removed a previous pin — outputs auto-detect again (reboot to apply)"
fi
fi
# Not gated: /sys/class/drm is kernel-generic, so this reports connected outputs
# on a PC's iGPU exactly as it does on the Pi.
for s in "$DRM"/card*-HDMI-A-*/status; do
  [ -e "$s" ] || continue
  n="${s%/status}"; n="${n##*/}"
  echo "   ${n#*-}: $(cat "$s")"
done

echo "== code"
sudo mkdir -p "$OPT"
sudo chown "$USER" "$OPT"
if [ -d "$OPT/current/.git" ]; then
  git -C "$OPT/current" pull --ff-only
elif [ -e "$OPT/current" ]; then
  # After any auto-update `current` is a symlink into releases/<tag>, which
  # update.sh built with `git archive` and so has no .git. The else branch then
  # ran `git clone` into a path that already exists -- fatal, exit 128, and
  # under `set -e` that ended the install at "== code", after apt and the
  # raspi-config edits, with nothing installed and no banner. The header says
  # "Re-runnable" and uninstall.sh points here as the reinstall path.
  #
  # Leave it alone: update.sh owns that symlink and the release it points at,
  # and a working auto-updated box does not need its code replaced by a re-run
  # of the installer. Everything after this point is idempotent.
  echo "   $OPT/current is managed by update.sh — leaving the code alone"
else
  git clone "$REPO" "$OPT/current"
fi
cd "$OPT/current"
[ -d .venv ] || python3 -m venv .venv
# Checked, not assumed. This used to run under `set -e` with no message of its
# own: a wheel that failed to build left the venv half-populated, the script
# carried on and enabled the service, and the first you heard of it was a
# journalctl dump at the end. Say which step failed, at the step that failed.
if ! .venv/bin/pip install -q --require-hashes -r agent/requirements.txt; then
  echo "pip install failed — not enabling the service. Fix the error above and" >&2
  echo "re-run this script; nothing before this point needs undoing." >&2
  exit 1
fi

echo "== config"
sudo mkdir -p "$ETC"
if [ -f "$CFG" ]; then
  echo "   $CFG exists, left alone"
else
  # home_url is the one that has to be the *tailnet* url, not the shipped
  # "/home". The unit passes --host "$(tailscale ip -4)" and never reads
  # [server], so the path form resolves to http://127.0.0.1:8080/home -- a port
  # nothing is listening on. Every screen then comes up on Chromium's error
  # page, /v1/inspect reports error_page: true forever, and update.sh's rollback
  # gate fires on *every* release and latches it. Auto-update was dead on
  # arrival on any box this script built. tests/test_install_roundtrip.py pins
  # that what lands here is loadable and points at the bound address.
  if [ -z "$TS_IP" ]; then
    echo "   no tailscale address — writing home_url as a loopback url. Fix it" >&2
    echo "   in $CFG once the node is up, or the kiosk shows an error page." >&2
    HOME_URL="http://127.0.0.1:$PORT/home"
  else
    HOME_URL="http://$TS_IP:$PORT/home"
  fi
  sed -e "s|^token = .*|token = \"$(openssl rand -hex 32)\"|" \
      -e "s|^home_url = .*|home_url = \"$HOME_URL\"|" \
      -e "s|^kind = .*|kind = \"chromium\"|" \
      -e "s|^profile_dir = .*|profile_dir = \"$RUN/profile\"|" \
      -e "s|^extensions_dir = .*|extensions_dir = \"$OPT/extensions\"|" \
      -e "s|^dir = .*|dir = \"$RUN/uploads\"|" \
      agent/config.example.toml | sudo tee "$CFG" >/dev/null
  echo "   home_url = $HOME_URL"
fi
sudo chown root:"$USER" "$CFG"
sudo chmod 640 "$CFG"                      # it holds the bearer token
mkdir -p "$DATA"

# Pi only: this trades persistent logs for SD card life. A server logs to an SSD
# that does not care, and taking a hypervisor's journal away to save writes it
# can afford is a bad trade.
if [ "$IS_PI" = 1 ]; then
echo "== logs in RAM"
# See journald-volatile.conf for why this rather than log2ram, and what it costs.
sudo mkdir -p "$JOURNALD_D"
sudo cp deploy/pi/journald-volatile.conf "$JOURNALD_D/crossdrop.conf"
sudo systemctl restart systemd-journald
fi

# Empty is fine and is the normal state: the agent loads whatever is in here at
# launch, so this only has to exist for install-extension.sh to drop into.
mkdir -p "$OPT/extensions"

echo "== service"
chmod +x deploy/pi/profile-snapshot.sh deploy/pi/update.sh
mkdir -p "$UNITS"
cp deploy/pi/crossdrop-agent.service "$UNITS/"
# Timer stays installed-but-disabled: PLAN.md §9 default is snapshot-on-stop.
# Enable it if the study loses power often: systemctl --user enable --now crossdrop-snapshot.timer
cp deploy/pi/crossdrop-snapshot.service deploy/pi/crossdrop-snapshot.timer "$UNITS/"
# Phase 8 auto-update, also installed-but-disabled. Handing a Pi the right to
# replace its own code unattended is a decision to make on purpose, not a side
# effect of running a setup script:
#   systemctl --user enable --now crossdrop-update.timer
cp deploy/pi/crossdrop-update.service deploy/pi/crossdrop-update.timer "$UNITS/"
# This one *is* enabled: Chromium left on one page for days grows until it OOMs,
# and the 04:00 restart is the only thing standing between that and a wall
# showing "Aw, Snap!" until somebody carries a keyboard to it.
cp deploy/pi/crossdrop-restart.service deploy/pi/crossdrop-restart.timer "$UNITS/"
systemctl --user daemon-reload
systemctl --user enable --now crossdrop-agent crossdrop-restart.timer

# An existing config is never rewritten, so a Pi provisioned before Phase 6 still
# points its profile at the SD card and silently keeps grinding it.
if ! sudo grep -q "^profile_dir = \"$RUN/" "$CFG"; then
  echo "   NOTE: profile_dir in $CFG is not on tmpfs — Phase 6 is not active."
  echo "         Set it to $RUN/profile and restart."
fi
# Same class of problem, and the expensive one: a config written before this
# script set home_url points the kiosk at 127.0.0.1, which nothing binds. Every
# screen sits on an error page and update.sh rolls back every release.
if sudo grep -qE '^home_url = "(/|http://127\.0\.0\.1)' "$CFG"; then
  echo "   WARNING: home_url in $CFG resolves to loopback, but the unit binds"
  echo "            $TS_IP. The kiosk will show an error page and auto-update"
  echo "            will roll back every release. Set it to:"
  echo "              home_url = \"http://$TS_IP:$PORT/home\""
fi

# Diagnostics only. Nothing below may abort the script: the install is already
# done by this point, and `set -e` turning a failed *check* into a failed *run*
# is what hid the summary and the token the first time.
echo "== checks"
if findmnt -no FSTYPE "$(dirname "$RUN")" 2>/dev/null | grep -qx tmpfs; then
  echo "   profile + uploads on tmpfs: ok"
else
  echo "   WARNING: $(dirname "$RUN") is not tmpfs - Phase 6 buys you nothing"
fi
if [ -n "$CHROMIUM" ]; then
  echo "   $("$CHROMIUM" --version)"
else
  echo "   WARNING: no chromium binary found - set browser.path in $CFG"
fi
# Display power needs an X session and xset; on Wayland the agent leaves the
# monitors to the compositor, which on a keyboard-less box means they can blank
# with nothing able to wake them. Worth saying out loud at install time.
if DISPLAY=:0 xset q >/dev/null 2>&1; then
  echo "   display power: ok (X11) - agent sleeps/wakes the monitors"
else
  echo "   NOTE: no X session on :0 - agent will not manage display power."
  echo "         On Wayland, monitors may blank with no keyboard to wake them."
fi
sleep 5
if systemctl --user is-active --quiet crossdrop-agent; then
  echo "   crossdrop-agent: active"
else
  echo "   crossdrop-agent is NOT active:"
  journalctl --user -u crossdrop-agent -n 20 --no-pager || true
fi

# The token is deliberately *not* printed. It used to be, for the copy-paste
# convenience of the line below -- which also wrote the one credential this box
# has into terminal scrollback, a `script` log, and whatever the terminal
# emulator keeps. Printing the command that reads it costs one extra step and
# leaves the secret in the file it already lives in.
cat <<EOF

Done. From a controller box, with the token this prints (do not paste it into
anything that keeps history):

  sudo sed -n 's|^token = "\\(.*\\)"|\\1|p' $CFG

  curl -H "Authorization: Bearer \$TOKEN" http://$TS_IP:$PORT/v1/status

Or from this box, without the token ever being on screen:

  curl -sH "Authorization: Bearer \$(sudo sed -n 's|^token = "\\(.*\\)"|\\1|p' $CFG)" \\
       http://$TS_IP:$PORT/v1/status

Still on you: disable this node's key expiry in the Tailscale admin console,
or the Pi silently drops off the tailnet in ~6 months with no keyboard to fix it.

Reboot now to prove autologin + kiosk come up unattended:  sudo reboot
EOF
