#!/usr/bin/env bash
# Undoes deploy/pi/setup.sh, so a re-run of setup.sh starts from nothing. Run on
# the box, over SSH, as the user that owns the graphical session:
#
#   bash /opt/crossdrop/current/deploy/pi/uninstall.sh
#   bash deploy/pi/uninstall.sh -y        # from a clone; skip the prompt
#
# This deletes the bearer token, the browser logins and the screen settings —
# a reinstall generates a new token and starts logged out of everything.
#
# NOT removed, because they are not this project's to take away, and setup.sh
# reuses them: apt packages (chromium, python3-venv, xorg, openbox), Tailscale
# and its node key, and raspi-config's autologin/blanking settings.
#
# The rule this file follows throughout: **remove only what setup.sh created.**
# Anything that merely looks like ours -- a video= pin somebody else set, a
# console autologin from raspi-config, a hand-written .xinitrc -- is left alone,
# because undoing a setting you never made is worse than leaving one behind, and
# both are read at boot on a box with no keyboard.
set -euo pipefail

# PREFIX exists so tests/test_install_roundtrip.py can run this script for real
# against a temp tree. Empty in production, and every path below is then the
# absolute one it always was. Do not add a path here that is not derived from it.
PREFIX="${PREFIX:-}"
OPT="${OPT:-$PREFIX/opt/crossdrop}"
ETC="${ETC:-$PREFIX/etc/crossdrop}"
RUN="${RUN:-$PREFIX/run/user/$(id -u)/crossdrop}"
SYSTEMD_SYS="${SYSTEMD_SYS:-$PREFIX/etc/systemd/system}"
JOURNALD_D="${JOURNALD_D:-$PREFIX/etc/systemd/journald.conf.d}"
CMDLINE="${CMDLINE:-$PREFIX/boot/firmware/cmdline.txt}"
UNITS="${UNITS:-$HOME/.config/systemd/user}"
# Honoured here as well as in the agent and profile-snapshot.sh. A box that moved
# its data dir keeps profile.tar.gz -- i.e. the browser logins, the one thing the
# header above promises to delete -- if this hardcodes the default.
DATA="${CROSSDROP_DATA:-$HOME/.local/share/crossdrop}"
# CROSSDROP_DATA is set by `Environment=` in the *unit file*, which is the
# documented way to move the data dir -- not in the SSH shell this runs from. So
# read the unit too, or a box that moved its data dir keeps profile.tar.gz (the
# browser logins) while this prints "Gone." and its header promises otherwise.
# The `|| true` is load-bearing, and its absence was silent. `set -o pipefail`
# makes this pipeline take sed's exit status, and sed exits 2 on a unit file
# that is not there -- which is the normal case. Under `set -e` the assignment
# below then ended the script, with no message at all, because sed's stderr is
# redirected. A clean box could not be uninstalled twice.
_unit_data() {
  [ -f "$UNITS/$1" ] || return 0
  sed -n "s|^Environment=$2=||p" "$UNITS/$1" 2>/dev/null | tail -1 || true
}
# `if`, not `[ ... ] && x=y`. That idiom returns false whenever the test fails,
# and under `set -e` a false AND-list is a dead script -- silently, because
# nothing printed. It killed this file here, before anything was deleted.
_from_unit="$(_unit_data crossdrop-agent.service CROSSDROP_DATA)"
if [ -n "$_from_unit" ]; then DATA="${_from_unit/\%h/$HOME}"; fi
# The v1 layout, so this script can clean a box that was never migrated.
OLD_OPT="${OLD_OPT:-$PREFIX/opt/room-display}"
OLD_ETC="${OLD_ETC:-$PREFIX/etc/room-display}"
OLD_DATA="${OLD_DATA:-${ROOM_DATA:-$HOME/.local/share/room-display}}"
_from_unit="$(_unit_data display-agent.service ROOM_DATA)"
if [ -n "$_from_unit" ]; then OLD_DATA="${_from_unit/\%h/$HOME}"; fi
OLD_RUN="${OLD_RUN:-$PREFIX/run/user/$(id -u)/room-display}"

# bash reads a script as it runs, so deleting $OPT out from under ourselves
# truncates the rest of this file mid-run. Work from a copy. PREFIX travels in
# the environment, not as an argument: `env` passes the environment and "$@" is
# the user's own flags.
SELF="$(readlink -f "$0")"
case "$SELF" in
  "$OPT"/*)
    TMP="$(mktemp)"; cp "$SELF" "$TMP"
    exec env CROSSDROP_UNINSTALL_TMP="$TMP" bash "$TMP" "$@" ;;
esac
trap 'rm -f "${CROSSDROP_UNINSTALL_TMP:-/nonexistent}"' EXIT

if [ "$(id -u)" -eq 0 ]; then
  echo "run as the user that owns the graphical session, not root — the user" >&2
  echo "units and $DATA are yours, not root's." >&2
  exit 1
fi

# Whichever file actually holds the block, not whichever setup.sh *would* pick.
# setup.sh appends to .profile when .bash_profile does not exist; creating a
# .bash_profile afterwards then made this look at the empty one and leave the
# block in .profile forever, while printing "Gone."
PROF=""
for cand in "$HOME/.bash_profile" "$HOME/.profile"; do
  if [ -f "$cand" ] && grep -q '^# CrossDrop kiosk session' "$cand"; then
    PROF="$cand"; break
  fi
done
if [ -z "$PROF" ]; then
  PROF="$HOME/.bash_profile"; [ -f "$PROF" ] || PROF="$HOME/.profile"
fi
# Both layouts. After a migration the *old* uninstall.sh is gone from the box,
# so this one has to be able to clean a Pi that never migrated -- and a half-
# migrated box has some of each. Everything below is idempotent on a path that
# is not there.
PATHS=(
  "$OPT" "$OLD_OPT"
  "$ETC" "$OLD_ETC"
  "$DATA" "$OLD_DATA"
  "$RUN" "$OLD_RUN"
  "$UNITS/crossdrop-agent.service" "$UNITS/display-agent.service"
  "$JOURNALD_D/crossdrop.conf" "$JOURNALD_D/room-display.conf"
)
PATHS+=("$UNITS"/crossdrop-*.{service,timer})
PATHS+=("$UNITS"/room-display-*.{service,timer})

# setup.sh drops this beside the config when -- and only when -- it writes a
# video= pin of its own. Without it we cannot tell our pin from the admin's, and
# a pin removed at random is a box that boots with a dark monitor.
PIN_MARK="$ETC/.video-pin"
[ -f "$PIN_MARK" ] || PIN_MARK="$OLD_ETC/.video-pin"
# Likewise for tty1: /etc/systemd/system/getty@tty1.service.d/autologin.conf is
# also exactly what `raspi-config nonint do_boot_behaviour B2` writes, so match
# on the content setup.sh wrote rather than on the filename.
AUTOLOGIN="$SYSTEMD_SYS/getty@tty1.service.d/autologin.conf"
ours_autologin() {
  [ -f "$AUTOLOGIN" ] && grep -q -- '--autologin' "$AUTOLOGIN" \
    && grep -q -- '--noclear %I \$TERM' "$AUTOLOGIN"
}

# Read *before* anything is deleted: the marker lives in $ETC, and $ETC is
# removed several steps above the pin check that needs it.
HAD_PIN=0; if [ -f "$PIN_MARK" ]; then HAD_PIN=1; fi
HAD_AUTOLOGIN=0; if ours_autologin; then HAD_AUTOLOGIN=1; fi

echo "This will delete — including the token, the logins and the settings:"
for p in "${PATHS[@]}"; do
  case "$p" in *'*'*) continue ;; esac          # an unmatched glob, not a path
  [ -e "$p" ] && echo "   $p" || echo "   $p  (already gone)"
done
[ -f "$PROF" ] && grep -q '^# CrossDrop kiosk session' "$PROF" \
  && echo "   the CrossDrop kiosk block in $PROF"
[ "$HAD_AUTOLOGIN" = 1 ] && echo "   the tty1 autologin $AUTOLOGIN"
[ "$HAD_PIN" = 1 ] && echo "   the video= pin in $CMDLINE"
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
# Stopping crossdrop-agent takes the browser with it (systemd kills the cgroup).
# Ordering matters: disable while the unit files are still there, or the
# default.target.wants/ and timers.target.wants/ symlinks are left dangling.
systemctl --user disable --now crossdrop-agent crossdrop-restart.timer \
  crossdrop-snapshot.timer crossdrop-update.timer 2>/dev/null || true
# The v1 unit names too, for a box that was never migrated -- after a migration
# the old uninstall.sh is gone, so this script is the only one left that can.
systemctl --user disable --now display-agent room-display-restart.timer \
  room-display-snapshot.timer room-display-update.timer 2>/dev/null || true
rm -f "$UNITS/crossdrop-agent.service" "$UNITS"/crossdrop-*.{service,timer} \
      "$UNITS/display-agent.service" "$UNITS"/room-display-*.{service,timer}
# Guarded, like the two either side of it. `systemctl --user` needs a session
# bus, and over SSH there often is not one (deploy/pi/update-over-ssh.md §"Failed
# to connect to bus"). Unguarded under `set -e` this killed the run *here* --
# after the units were deleted and before the token, the profile snapshot, the
# journald drop-in and the kiosk block were touched. A half-uninstalled box, and
# the "Gone." banner never printed to say otherwise.
systemctl --user daemon-reload 2>/dev/null \
  || echo "   note: could not reload the user manager (no session bus?) — the" \
          "unit files are gone, so this only matters until the next login." >&2
systemctl --user reset-failed 2>/dev/null || true

echo "== code, config and data"
sudo rm -rf "$OPT" "$ETC" "$OLD_OPT" "$OLD_ETC"
rm -rf "$DATA" "$RUN" "$OLD_DATA" "$OLD_RUN"

if [ -f "$JOURNALD_D/crossdrop.conf" ] || [ -f "$JOURNALD_D/room-display.conf" ]; then
  echo "== logs back on disk"
  sudo rm -f "$JOURNALD_D/crossdrop.conf" "$JOURNALD_D/room-display.conf"
  # The directory is shared with anything else that drops a journald config, so
  # rmdir only if we left it empty. Failing is fine and expected.
  sudo rmdir "$JOURNALD_D" 2>/dev/null || true
  sudo systemctl restart systemd-journald 2>/dev/null || true
fi

if [ "$HAD_AUTOLOGIN" = 1 ]; then
  echo "== tty1 autologin"        # the Debian-box path; a Pi uses raspi-config
  sudo rm -f "$AUTOLOGIN"
  sudo rmdir "$SYSTEMD_SYS/getty@tty1.service.d" 2>/dev/null || true
  sudo systemctl daemon-reload 2>/dev/null || true
fi

if [ -f "$PROF" ] && grep -q '^# CrossDrop kiosk session' "$PROF"; then
  echo "== kiosk block in $PROF"
  # A sed range whose end address never matches deletes to end of file, so the
  # end has to be proven present *before* the sed runs -- and proven in the same
  # region the sed will search, which is from the header onwards.
  #
  # Two earlier versions of this guard checked the whole file and were both
  # bypassable. Grepping the file for `exec startx` passes on the box the Debian
  # branch targets, because an admin who already auto-started X on tty1 has that
  # string above our header; the range then runs unterminated and eats
  # everything they own. Scope the check to the block itself.
  if sed -n '/^# CrossDrop kiosk session/,$p' "$PROF" | grep -q 'exec startx'; then
    sed -i '/^# CrossDrop kiosk session/,/exec startx/d' "$PROF"
  else
    echo "   the block has been edited (no 'exec startx' after its header) —" >&2
    echo "   a range with no end would delete the rest of the file, so it is" >&2
    echo "   left in place. Remove it by hand: $PROF" >&2
  fi
fi
# Only the one setup.sh writes. A hand-written .xinitrc is somebody's own work.
if [ "$(cat "$HOME/.xinitrc" 2>/dev/null)" = "exec openbox-session" ]; then
  rm -f "$HOME/.xinitrc"
fi

# Only a pin we placed. setup.sh leaves $PIN_MARK when it writes one; a pin with
# no marker beside it belongs to whoever set up this box, and the "boots with no
# monitor attached" case in deploy/pi/pi-setup.md is a legitimate reason to have
# one that predates this project entirely.
if [ "$HAD_PIN" = 1 ] && [ -f "$CMDLINE" ] && grep -q 'video=' "$CMDLINE"; then
  echo "== display pin"
  sudo sed -i '1s| video=[^ ]*||g' "$CMDLINE"     # single line, edit in place
fi

cat <<'EOF'

Gone. Still installed, on purpose: chromium, python3-venv, git, xorg/openbox,
Tailscale (your way back in), and the Pi's autologin/blanking settings. Any
video= pin this project did not place is also left alone.

Reinstall from a clean state:

  curl -fsSL https://raw.githubusercontent.com/Kramav/CrossDrop/main/deploy/pi/setup.sh | bash

Reboot first if you removed a video= pin or the tty1 autologin — both are read
at boot, and setup.sh will not see them change under it.
EOF
