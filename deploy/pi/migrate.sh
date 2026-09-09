#!/usr/bin/env bash
# Moves a v1 install (room-display / display-agent) to the v2 layout
# (crossdrop / crossdrop-agent). Run on the box, over SSH, as the user that
# owns the graphical session:
#
#   bash /opt/room-display/current/deploy/pi/migrate.sh
#
# Why this exists as its own script rather than something update.sh does:
#
#   1. The update timer's ExecStart is /opt/room-display/current/deploy/pi/
#      update.sh, and update.sh has never rewritten unit files -- only setup.sh
#      does. So the frozen unit always runs the *old* path, whatever tag is
#      deployed. A new update.sh in a new root would simply never be the one the
#      timer executes.
#   2. bash reads a script as it runs. `mv /opt/room-display` from inside a file
#      under /opt/room-display truncates everything after the rename -- including
#      the swap, the verify and the rollback.
#   3. WorkingDirectory=/opt/room-display/current in the frozen unit. After the
#      move, `systemctl --user restart` fails with 200/CHDIR -- and update.sh's
#      rollback issues that same restart, so the rollback fails identically.
#      That is the case where somebody has to walk into the room.
#
# The design rule here: **the code is disposable, the state is not.** This moves
# the two things that cannot be regenerated -- the bearer token and the browser
# profile snapshot, which is what keeps you logged in -- backs them up, and then
# hands off to setup.sh, which is idempotent, leaves an existing config alone,
# and is the thing tests/test_install_roundtrip.py actually covers. /opt is
# rebuilt from git rather than moved, because it is a checkout and nothing else.
set -euo pipefail

PREFIX="${PREFIX:-}"
OLD_OPT="${OLD_OPT:-$PREFIX/opt/room-display}"
OLD_ETC="${OLD_ETC:-$PREFIX/etc/room-display}"
OLD_DATA="${OLD_DATA:-${ROOM_DATA:-$HOME/.local/share/room-display}}"
OLD_RUN="${OLD_RUN:-$PREFIX/run/user/$(id -u)/room-display}"
NEW_OPT="${NEW_OPT:-$PREFIX/opt/crossdrop}"
NEW_ETC="${NEW_ETC:-$PREFIX/etc/crossdrop}"
NEW_DATA="${NEW_DATA:-${CROSSDROP_DATA:-$HOME/.local/share/crossdrop}}"
JOURNALD_D="${JOURNALD_D:-$PREFIX/etc/systemd/journald.conf.d}"
UNITS="${UNITS:-$HOME/.config/systemd/user}"
REPO="${REPO:-}"
SETUP="${SETUP:-}"

# Same self-copy guard as uninstall.sh, and for the same reason: this deletes
# $OLD_OPT, and it may well be running from inside it.
SELF="$(readlink -f "$0")"
case "$SELF" in
  "$OLD_OPT"/*)
    TMP="$(mktemp)"; cp "$SELF" "$TMP"
    exec env CROSSDROP_MIGRATE_TMP="$TMP" bash "$TMP" "$@" ;;
esac
trap 'rm -f "${CROSSDROP_MIGRATE_TMP:-/nonexistent}"' EXIT

if [ "$(id -u)" -eq 0 ]; then
  echo "run as the user that owns the graphical session, not root." >&2
  exit 1
fi

# --- 1. preconditions, before anything is touched --------------------------
# Fail here or not at all: every step after this point moves something.
# Resumable, deliberately. Refusing when $NEW_OPT exists made setup.sh and this
# script refuse each other: setup.sh sends a half-migrated box here (its own
# guard is only `[ -d "$OLD_OPT" ]`, on purpose), and this then bounced it back.
# Both dead ends are reached *after* `systemctl --user disable --now
# display-agent` has already run, so the wall is dark and no documented command
# brings it back.
#
# Every step below is a move or a mkdir, so re-running over a partial state is
# safe: what has moved stays moved, what has not moves now.
if [ ! -d "$OLD_OPT" ] && [ ! -d "$OLD_ETC" ] && [ ! -d "$NEW_OPT" ]; then
  echo "nothing here to migrate: no $OLD_OPT and no $NEW_OPT" >&2
  exit 1
fi
if [ ! -d "$OLD_OPT" ] && [ -d "$NEW_OPT" ] && [ -f "$NEW_ETC/config.toml" ]; then
  # The state moved, but did the code arrive? An interrupt during the hand-off
  # to setup.sh -- a `git clone` plus a pip install, i.e. minutes, i.e. one
  # dropped SSH session -- leaves the old tree gone, no code at $NEW_OPT/current
  # and display-agent already disabled. Reporting "already migrated" and exiting
  # 0 there told the operator success while the wall was dark.
  if [ -f "$NEW_OPT/current/agent/app.py" ]; then
    echo "already migrated: $NEW_OPT exists and $OLD_OPT does not." >&2
    echo "If the box is not working, run setup.sh to rebuild the code." >&2
    exit 0
  fi
  echo "the state was migrated but the code never arrived at $NEW_OPT/current." >&2
  echo "Finishing that step now." >&2
  NEEDS_CODE_ONLY=1
fi
NEEDS_CODE_ONLY="${NEEDS_CODE_ONLY:-0}"
if [ -e "$NEW_OPT" ]; then
  echo "   resuming a partial migration ($NEW_OPT already exists)"
fi
# The config is the one thing that must be somewhere. Either side is fine.
if [ ! -f "$OLD_ETC/config.toml" ] && [ ! -f "$NEW_ETC/config.toml" ]; then
  echo "no config at $OLD_ETC/config.toml or $NEW_ETC/config.toml" >&2
  exit 1
fi
# The body reads $NEW_ETC/config.toml, and the mv that creates it is skipped when
# $NEW_ETC already exists. So an existing-but-empty $NEW_ETC used to pass this
# block and then die at the `cp` -- *after* the units were stopped, which is the
# failure the "Fail here or not at all" rule above exists to exclude.
if [ -d "$NEW_ETC" ] && [ ! -f "$NEW_ETC/config.toml" ]    && [ ! -f "$OLD_ETC/config.toml" ]; then
  echo "$NEW_ETC exists but holds no config.toml, and neither does $OLD_ETC." >&2
  echo "Nothing has been changed. Restore a config to either path first." >&2
  exit 1
fi
# The bare cache clone FIRST, the way update.sh does it, because on the box most
# likely to be migrated `current` has no remote to ask. Any box that has ever
# auto-updated has `current` as a symlink into releases/<tag>, and update.sh
# builds those with `git archive | tar -x` deliberately -- a release is code and
# nothing else, so there is no .git in it. Only checking `current` meant the
# common case failed with "no repo url: set REPO=...", and /v1/status reporting a
# tag rather than "dev" is the tell that a box is in exactly that state.
[ -n "$REPO" ] || REPO="$(git -C "$OLD_OPT/cache-repo" remote get-url origin 2>/dev/null || true)"
[ -n "$REPO" ] || REPO="$(git -C "$NEW_OPT/cache-repo" remote get-url origin 2>/dev/null || true)"
[ -n "$REPO" ] || REPO="$(git -C "$OLD_OPT/current" remote get-url origin 2>/dev/null || true)"
[ -n "$REPO" ] || REPO="$(git -C "$NEW_OPT/current" remote get-url origin 2>/dev/null || true)"
# And the same default setup.sh carries, so the documented one-liner works with
# no arguments on a stock box. Overriding REPO= still wins, which is what a fork
# or a private mirror needs.
[ -n "$REPO" ] || REPO="https://github.com/Kramav/CrossDrop.git"
sudo -v

# --- 2. fetch the new code BEFORE destroying the old --------------------------
# $OLD_OPT used to be deleted ten lines before the clone that replaces it. A
# repo that has gone private, an ssh remote with no key on the Pi, or a network
# blip then left the box with no code, no units and no way to be told so.
# Clone first: if this fails, nothing has been touched yet.
if [ -z "$SETUP" ]; then
  STAGE="$(mktemp -d)"
  trap 'rm -f "${CROSSDROP_MIGRATE_TMP:-/nonexistent}"; rm -rf "$STAGE"' EXIT
  echo "== fetching $REPO"
  if ! git clone --depth 1 "$REPO" "$STAGE/repo"; then
    echo "could not clone $REPO — nothing has been changed. Fix the remote (or" >&2
    echo "set REPO=...) and run this again." >&2
    exit 1
  fi
  SETUP="$STAGE/repo/deploy/pi/setup.sh"
fi
[ -f "$SETUP" ] || { echo "no setup.sh at $SETUP" >&2; exit 1; }

# The data dir the *agent* actually uses, which is the unit's Environment= if it
# has one -- not this shell's, where CROSSDROP_DATA/ROOM_DATA is never set.
# Reading only the environment stranded profile.tar.gz (the browser logins) on
# any box that had moved it: migrate reported success, and the display came up
# logged out of everything with nobody able to type the passwords back in.
# `|| true`: pipefail makes this pipeline take sed's status, and sed exits 2 on
# a unit file that is not there. Under `set -e` that ends the script silently,
# because sed's stderr is redirected -- see the same note in uninstall.sh.
UNIT_DATA="$(sed -n 's|^Environment=ROOM_DATA=||p' \
  "$UNITS/display-agent.service" 2>/dev/null | tail -1 || true)"
if [ -n "$UNIT_DATA" ]; then
  OLD_DATA="${UNIT_DATA/\%h/$HOME}"
  echo "   data dir from the unit: $OLD_DATA"
fi

echo "== what is running now"
# Recorded, so the optional timers come back the way they were. The restart
# timer is enabled by setup.sh regardless, so it is not in this list.
WAS_SNAPSHOT=0; systemctl --user is-enabled room-display-snapshot.timer >/dev/null 2>&1 \
  && WAS_SNAPSHOT=1
WAS_UPDATE=0; systemctl --user is-enabled room-display-update.timer >/dev/null 2>&1 \
  && WAS_UPDATE=1
echo "   snapshot timer: $WAS_SNAPSHOT, update timer: $WAS_UPDATE"

echo "== stopping the old units"
# The update timer first and by itself: it fires every 30 minutes, and one
# firing in the middle of this would deploy a release into the tree being moved.
systemctl --user disable --now room-display-update.timer 2>/dev/null || true
systemctl --user disable --now display-agent room-display-restart.timer \
  room-display-snapshot.timer 2>/dev/null || true

echo "== config"
if [ -d "$OLD_ETC" ] && [ ! -d "$NEW_ETC" ]; then
  sudo mv "$OLD_ETC" "$NEW_ETC"
elif [ -d "$OLD_ETC" ]; then
  # Both exist: a previous run moved it and was interrupted, or somebody made
  # $NEW_ETC by hand. Whichever holds a config is the live one.
  if [ ! -f "$NEW_ETC/config.toml" ] && [ -f "$OLD_ETC/config.toml" ]; then
    echo "   taking config.toml from $OLD_ETC"
    sudo cp -p "$OLD_ETC/config.toml" "$NEW_ETC/config.toml"
  else
    echo "   $NEW_ETC already has a config; keeping it"
  fi
  # And drop the leftover rather than leaving the old bearer token on disk
  # forever -- the token is never regenerated, so that copy stays valid.
  sudo rm -rf "$OLD_ETC"
fi
# The token, kept where a human can find it if the verify at the end fails.
# Never overwritten: on a resume the live config is already migrated, and
# clobbering the backup with it would lose the only copy of what was there
# before.
if [ ! -f "$NEW_ETC/config.toml.pre-crossdrop" ]; then
  sudo cp -p "$NEW_ETC/config.toml" "$NEW_ETC/config.toml.pre-crossdrop"
fi
# Three keys hold the old name. profile_dir and the upload dir are under
# /run/user, which is tmpfs and empty at every boot, so rewriting them costs
# nothing; extensions_dir is on the SD card and moves with $OLD_OPT below.
sudo sed -i 's|/room-display/|/crossdrop/|g; s|/opt/room-display|/opt/crossdrop|g' \
  "$NEW_ETC/config.toml"
# And the one the v1 installer never wrote at all. Every box in the field has
# `home_url = "/home"`, which resolves against [server] to 127.0.0.1 while the
# unit binds the tailnet address -- so the kiosk sits on an error page and
# update.sh rolls back every release. setup.sh leaves an existing config alone,
# by design, so a migration that only renamed paths would carry the defect
# across intact and report success.
if sudo grep -qE '^home_url = "(/|http://127\.0\.0\.1)' "$NEW_ETC/config.toml"; then
  TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
  if [ -n "$TS_IP" ]; then
    sudo sed -i "s|^home_url = .*|home_url = \"http://$TS_IP:${PORT:-8080}/home\"|" \
      "$NEW_ETC/config.toml"
    echo "   home_url was loopback (the v1 default) — now http://$TS_IP:${PORT:-8080}/home"
  else
    echo "   WARNING: home_url is loopback and tailscale has no address, so it" >&2
    echo "   cannot be fixed here. The kiosk will show an error page until you" >&2
    echo "   set it in $NEW_ETC/config.toml." >&2
  fi
fi

echo "== data (the logins)"
if [ -d "$OLD_DATA" ] && [ ! -d "$NEW_DATA" ]; then
  mkdir -p "$(dirname "$NEW_DATA")"
  mv "$OLD_DATA" "$NEW_DATA"
else
  mkdir -p "$NEW_DATA"
fi

echo "== extensions"
# On the SD card rather than tmpfs, so these survive a boot and are worth
# carrying. Moved before $OLD_OPT is deleted.
if [ -d "$OLD_OPT/extensions" ] && [ ! -d "$NEW_OPT/extensions" ]; then
  sudo mkdir -p "$NEW_OPT"
  sudo mv "$OLD_OPT/extensions" "$NEW_OPT/extensions"
  sudo chown -R "$(id -un)" "$NEW_OPT"
fi

echo "== journald drop-in"
if [ -f "$JOURNALD_D/room-display.conf" ]; then
  sudo mv "$JOURNALD_D/room-display.conf" "$JOURNALD_D/crossdrop.conf"
fi

echo "== releases"
# A tag that failed verify under the old layout is latched forever. Cleared
# here, while $OLD_OPT still exists -- doing it after the rm below was a no-op
# against a directory that had just been deleted, and setup.sh never recreates
# releases/, so the marker would have come back with the first update instead.
sudo rm -f "$OLD_OPT"/releases/.failed-* 2>/dev/null || true

echo "== old units and code"
rm -f "$UNITS/display-agent.service" "$UNITS"/room-display-*.{service,timer}
systemctl --user daemon-reload 2>/dev/null || true
systemctl --user reset-failed 2>/dev/null || true
sudo rm -rf "$OLD_OPT"
rm -rf "$OLD_RUN"

echo "== rebuilding at $NEW_OPT"
# Hand off to setup.sh rather than reimplementing it: idempotent, leaves the
# config we just moved alone, and it is the half with a round-trip test behind
# it. UPGRADE=0 explicitly -- setup.sh defaults it to 1 on a Pi, and "rename the
# install" must not silently become "full-upgrade the kernel under it", which
# also runs past sudo's 15-minute timestamp. Upgrade separately if you want to.
UPGRADE="${UPGRADE:-0}" REPO="$REPO" bash "$SETUP"

echo "== restoring optional timers"
# Guarded like every other systemctl here. Unguarded under `set -e` these were
# the only two that could kill the run -- at the very end, after a *successful*
# migration, so the operator saw a bus error and never learned where the box
# was, where the config backup went, or that migrate.sh now refuses to re-run.
[ "$WAS_SNAPSHOT" = 1 ] && { systemctl --user enable --now crossdrop-snapshot.timer \
  || echo "   note: could not enable crossdrop-snapshot.timer" >&2; }
[ "$WAS_UPDATE" = 1 ] && { systemctl --user enable --now crossdrop-update.timer \
  || echo "   note: could not enable crossdrop-update.timer" >&2; }
:   # the two tests above are the last command; without this `set -e` exits 1

cat <<EOF

Migrated. The box is now:

  code    $NEW_OPT
  config  $NEW_ETC/config.toml     (token unchanged — your targets.toml still works)
  data    $NEW_DATA
  units   crossdrop-agent, crossdrop-{restart,snapshot,update}.timer

  systemctl --user status crossdrop-agent
  journalctl --user -u crossdrop-agent -n 50

The token was not regenerated, so every controller keeps working. A backup of
the config as it was is at $NEW_ETC/config.toml.pre-crossdrop.

If something is wrong, nothing was deleted except the git checkout: the config,
the logins and the extensions were all moved, not recreated.
EOF
