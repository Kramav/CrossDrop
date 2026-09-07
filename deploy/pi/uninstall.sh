#!/usr/bin/env bash
# Undoes deploy/pi/setup.sh, so a re-run of setup.sh starts from nothing. Run on
# the box, over SSH, as the user that owns the graphical session:
#
#   bash /opt/room-display/current/deploy/pi/uninstall.sh
#   bash deploy/pi/uninstall.sh -y        # from a clone; skip the prompt
#
# This deletes the bearer token, the browser logins and the screen settings —
# a reinstall generates a new token and starts logged out of everything.
#
# NOT removed, because they are not this project's to take away, and setup.sh
# reuses them: apt packages (chromium, python3-venv, xorg, openbox), Tailscale
# and its node key, and raspi-config's autologin/blanking settings.
set -euo pipefail

# bash reads a script as it runs, so deleting /opt/room-display out from under
# ourselves truncates the rest of this file mid-run. Work from a copy.
SELF="$(readlink -f "$0")"
case "$SELF" in
  /opt/room-display/*)
    TMP="$(mktemp)"; cp "$SELF" "$TMP"
    exec env ROOM_UNINSTALL_TMP="$TMP" bash "$TMP" "$@" ;;
esac
trap 'rm -f "${ROOM_UNINSTALL_TMP:-/nonexistent}"' EXIT

if [ "$(id -u)" -eq 0 ]; then
  echo "run as the user that owns the graphical session, not root — the user" >&2
  echo "units and ~/.local/share/room-display are yours, not root's." >&2
  exit 1
fi

UID_="$(id -u)"
PROF="$HOME/.bash_profile"; [ -f "$PROF" ] || PROF="$HOME/.profile"
PATHS=(
  /opt/room-display
  /etc/room-display
  "$HOME/.local/share/room-display"
  "/run/user/$UID_/room-display"
  "$HOME/.config/systemd/user/display-agent.service"
  /etc/systemd/journald.conf.d/room-display.conf
  /etc/systemd/system/getty@tty1.service.d/autologin.conf
)
PATHS+=("$HOME"/.config/systemd/user/room-display-*.{service,timer})

echo "This will delete — including the token, the logins and the settings:"
for p in "${PATHS[@]}"; do
  case "$p" in *'*'*) continue ;; esac          # an unmatched glob, not a path
  [ -e "$p" ] && echo "   $p" || echo "   $p  (already gone)"
done
[ -f "$PROF" ] && grep -q '^# CrossDrop kiosk session' "$PROF" \
  && echo "   the CrossDrop kiosk block in $PROF"
grep -q 'video=' /boot/firmware/cmdline.txt 2>/dev/null \
  && echo "   the video= pin in /boot/firmware/cmdline.txt"
echo

if [ "${1:-}" != "-y" ]; then
  # Deliberately not readable from a pipe: `curl ... | bash` would answer this
  # prompt with the next line of the script.
  [ -t 0 ] || { echo "not a terminal — re-run with -y if you mean it" >&2; exit 1; }
  read -rp "type 'wipe' to go ahead: " answer
  [ "$answer" = wipe ] || { echo "aborted, nothing changed"; exit 1; }
fi

sudo -v

echo "== services"
# Stopping display-agent takes the browser with it (systemd kills the cgroup).
systemctl --user disable --now display-agent room-display-restart.timer \
  room-display-snapshot.timer room-display-update.timer 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/display-agent.service" \
      "$HOME"/.config/systemd/user/room-display-*.{service,timer}
systemctl --user daemon-reload
systemctl --user reset-failed 2>/dev/null || true

echo "== code, config and data"
sudo rm -rf /opt/room-display /etc/room-display
rm -rf "$HOME/.local/share/room-display" "/run/user/$UID_/room-display"

if [ -f /etc/systemd/journald.conf.d/room-display.conf ]; then
  echo "== logs back on disk"
  sudo rm -f /etc/systemd/journald.conf.d/room-display.conf
  sudo systemctl restart systemd-journald
fi

if [ -f /etc/systemd/system/getty@tty1.service.d/autologin.conf ]; then
  echo "== tty1 autologin"        # the Debian-box path; a Pi uses raspi-config
  sudo rm -f /etc/systemd/system/getty@tty1.service.d/autologin.conf
  sudo rmdir /etc/systemd/system/getty@tty1.service.d 2>/dev/null || true
  sudo systemctl daemon-reload
fi

if [ -f "$PROF" ] && grep -q '^# CrossDrop kiosk session' "$PROF"; then
  echo "== kiosk block in $PROF"
  sed -i '/^# CrossDrop kiosk session/,/exec startx/d' "$PROF"
fi
# Only the one setup.sh writes. A hand-written .xinitrc is somebody's own work.
if [ "$(cat "$HOME/.xinitrc" 2>/dev/null)" = "exec openbox-session" ]; then
  rm -f "$HOME/.xinitrc"
fi

CMDLINE=/boot/firmware/cmdline.txt
if [ -f "$CMDLINE" ] && grep -q 'video=' "$CMDLINE"; then
  echo "== display pin"
  sudo sed -i '1s| video=[^ ]*||g' "$CMDLINE"     # single line, edit in place
fi

cat <<'EOF'

Gone. Still installed, on purpose: chromium, python3-venv, git, xorg/openbox,
Tailscale (your way back in), and the Pi's autologin/blanking settings.

Reinstall from a clean state:

  curl -fsSL https://raw.githubusercontent.com/Kramav/CrossDrop/main/deploy/pi/setup.sh | bash

Reboot first if you removed a video= pin or the tty1 autologin — both are read
at boot, and setup.sh will not see them change under it.
EOF
