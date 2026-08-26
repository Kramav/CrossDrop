#!/usr/bin/env bash
# Provisions a display Pi from a freshly-imaged card to a running agent.
# Covers pi-setup.md §2-§7 and README.md §2-§5. Re-runnable.
#
#   curl -fsSL https://raw.githubusercontent.com/Kramav/CrossDrop/main/deploy/pi/setup.sh | bash
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

[ "$(id -u)" -ne 0 ] || { echo "run as your normal user, not root — the agent runs as you"; exit 1; }
sudo -v

echo "== packages"
sudo apt update
sudo apt full-upgrade -y
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

echo "== raspi-config"
sudo raspi-config nonint do_boot_behaviour B4   # desktop autologin
sudo raspi-config nonint do_blanking 1          # 1 = disable blanking. A kiosk must never sleep.
# Wayland compositor left alone: labwc is the default and is what the unit assumes.

echo "== display mode"
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

echo "== logs in RAM"
# See journald-volatile.conf for why this rather than log2ram, and what it costs.
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp deploy/pi/journald-volatile.conf /etc/systemd/journald.conf.d/room-display.conf
sudo systemctl restart systemd-journald

echo "== service"
chmod +x deploy/pi/profile-snapshot.sh
mkdir -p ~/.config/systemd/user
cp deploy/pi/display-agent.service ~/.config/systemd/user/
# Timer stays installed-but-disabled: PLAN.md §9 default is snapshot-on-stop.
# Enable it if the study loses power often: systemctl --user enable --now room-display-snapshot.timer
cp deploy/pi/room-display-snapshot.service deploy/pi/room-display-snapshot.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now display-agent

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
