#!/usr/bin/env bash
# Provisions a display box from a fresh install to a running agent. Two kinds:
# a Raspberry Pi (pi-setup.md §2-§7 and README.md §2-§5), or a plain Debian box
# with monitors on it — a Proxmox host is the case this was written for, see
# deploy/linux.md. Re-runnable.
#
#   curl -fsSL https://raw.githubusercontent.com/Kramav/CrossDrop/main/deploy/pi/setup.sh | bash
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

IS_PI=0
if command -v raspi-config >/dev/null 2>&1; then IS_PI=1; fi

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
# Pi only. Upgrading every package on a hypervisor — kernel included, under the
# VMs — is the admin's decision, not a side effect of installing a kiosk.
if [ "$IS_PI" = 1 ]; then sudo apt full-upgrade -y; fi
sudo apt install -y python3-venv git
# Trixie Pi OS ships Debian's `chromium`, which installs /usr/bin/chromium and
# NO /usr/bin/chromium-browser. Bookworm and earlier shipped Raspberry Pi's own
# `chromium-browser` build. Current name first, old name for an older card.
sudo apt install -y chromium || sudo apt install -y chromium-browser
CHROMIUM="$(command -v chromium || command -v chromium-browser || true)"

echo "== tailscale"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
tailscale ip -4 >/dev/null 2>&1 || sudo tailscale up --ssh   # prints a URL; open it
TS_IP="$(tailscale ip -4 | head -1)"
echo "   $TS_IP"

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
sudo mkdir -p /etc/systemd/system/getty@tty1.service.d
printf '[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin %s --noclear %%I $TERM\n' "$USER" \
  | sudo tee /etc/systemd/system/getty@tty1.service.d/autologin.conf >/dev/null
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
# Restart=always in display-agent.service is for.
[ "$(tty)" = /dev/tty1 ] && [ -z "${DISPLAY:-}" ] && exec startx -- -nocursor
EOF
fi
fi

echo "== display mode"
if [ "$IS_PI" = 1 ]; then
CMDLINE=/boot/firmware/cmdline.txt
# ponytail: no pinning by default. The kernel reads EDID and brings every
# connected output up at its own preferred mode, which is the dynamic behaviour
# we want. Pinning one `video=HDMI-A-1:...` forces that output and leaves the
# second monitor dark — pin only on a Pi that boots with nothing plugged in.
if [ -n "$VIDEO" ]; then
  case "$VIDEO" in *:*) ;; *) echo "VIDEO must be <connector>:<mode>, e.g. HDMI-A-1:1920x1080@60D"; exit 1 ;; esac
  sudo sed -i -e '1s| video=[^ ]*||g' -e "1s|\$| video=$VIDEO|" "$CMDLINE"
  echo "   pinned: $VIDEO"
elif grep -q 'video=' "$CMDLINE"; then
  sudo sed -i '1s| video=[^ ]*||g' "$CMDLINE"              # single line, edit in place
  echo "   removed a previous pin — outputs auto-detect again (reboot to apply)"
fi
fi
# Not gated: /sys/class/drm is kernel-generic, so this reports connected outputs
# on a PC's iGPU exactly as it does on the Pi.
for s in /sys/class/drm/card*-HDMI-A-*/status; do
  [ -e "$s" ] || continue
  n="${s%/status}"; n="${n##*/}"
  echo "   ${n#*-}: $(cat "$s")"
done

echo "== code"
sudo mkdir -p /opt/room-display
sudo chown "$USER" /opt/room-display
if [ -d /opt/room-display/current/.git ]; then
  git -C /opt/room-display/current pull --ff-only
else
  git clone "$REPO" /opt/room-display/current
fi
cd /opt/room-display/current
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r agent/requirements.txt

echo "== config"
CFG=/etc/room-display/config.toml
sudo mkdir -p /etc/room-display
if [ -f "$CFG" ]; then
  echo "   $CFG exists, left alone"
else
  sed -e "s|^token = .*|token = \"$(openssl rand -hex 32)\"|" \
      -e "s|^kind = .*|kind = \"chromium\"|" \
      -e "s|^profile_dir = .*|profile_dir = \"/run/user/$(id -u)/room-display/profile\"|" \
      -e "s|^dir = .*|dir = \"/run/user/$(id -u)/room-display/uploads\"|" \
      agent/config.example.toml | sudo tee "$CFG" >/dev/null
fi
sudo chown root:"$USER" "$CFG"
sudo chmod 640 "$CFG"                      # it holds the bearer token
mkdir -p "$HOME/.local/share/room-display"

# Pi only: this trades persistent logs for SD card life. A server logs to an SSD
# that does not care, and taking a hypervisor's journal away to save writes it
# can afford is a bad trade.
if [ "$IS_PI" = 1 ]; then
echo "== logs in RAM"
# See journald-volatile.conf for why this rather than log2ram, and what it costs.
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp deploy/pi/journald-volatile.conf /etc/systemd/journald.conf.d/room-display.conf
sudo systemctl restart systemd-journald
fi

echo "== service"
chmod +x deploy/pi/profile-snapshot.sh deploy/pi/update.sh
mkdir -p ~/.config/systemd/user
cp deploy/pi/display-agent.service ~/.config/systemd/user/
# Timer stays installed-but-disabled: PLAN.md §9 default is snapshot-on-stop.
# Enable it if the study loses power often: systemctl --user enable --now room-display-snapshot.timer
cp deploy/pi/room-display-snapshot.service deploy/pi/room-display-snapshot.timer ~/.config/systemd/user/
# Phase 8 auto-update, also installed-but-disabled. Handing a Pi the right to
# replace its own code unattended is a decision to make on purpose, not a side
# effect of running a setup script:
#   systemctl --user enable --now room-display-update.timer
cp deploy/pi/room-display-update.service deploy/pi/room-display-update.timer ~/.config/systemd/user/
# This one *is* enabled: Chromium left on one page for days grows until it OOMs,
# and the 04:00 restart is the only thing standing between that and a wall
# showing "Aw, Snap!" until somebody carries a keyboard to it.
cp deploy/pi/room-display-restart.service deploy/pi/room-display-restart.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now display-agent room-display-restart.timer

# An existing config is never rewritten, so a Pi provisioned before Phase 6 still
# points its profile at the SD card and silently keeps grinding it.
if ! sudo grep -q '^profile_dir = "/run/user/' "$CFG"; then
  echo "   NOTE: profile_dir in $CFG is not on tmpfs — Phase 6 is not active."
  echo "         Set it to /run/user/$(id -u)/room-display/profile and restart."
fi

# Diagnostics only. Nothing below may abort the script: the install is already
# done by this point, and `set -e` turning a failed *check* into a failed *run*
# is what hid the summary and the token the first time.
echo "== checks"
if findmnt -no FSTYPE "/run/user/$(id -u)" | grep -qx tmpfs; then
  echo "   profile + uploads on tmpfs: ok"
else
  echo "   WARNING: /run/user/$(id -u) is not tmpfs - Phase 6 buys you nothing"
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
if systemctl --user is-active --quiet display-agent; then
  echo "   display-agent: active"
else
  echo "   display-agent is NOT active:"
  journalctl --user -u display-agent -n 20 --no-pager || true
fi

TOKEN="$(sudo sed -n 's|^token = "\(.*\)"|\1|p' "$CFG")"
cat <<EOF

Done. From a controller box:

  curl -H "Authorization: Bearer $TOKEN" http://$TS_IP:$PORT/v1/status

Still on you: disable this node's key expiry in the Tailscale admin console,
or the Pi silently drops off the tailnet in ~6 months with no keyboard to fix it.

Reboot now to prove autologin + kiosk come up unattended:  sudo reboot
EOF
