#!/usr/bin/env bash
# Browser profile <-> SD snapshot. Phase 6.
#
#   profile-snapshot.sh restore   # SD -> RAM, before the agent starts
#   profile-snapshot.sh save      # RAM -> SD, after it stops
#
# The profile lives on tmpfs (RAM) so a 24/7 kiosk stops grinding the SD card.
# Nothing in RAM survives a reboot, so the snapshot is also what keeps you
# logged in to a school page across one. Wired into display-agent.service as
# ExecStartPre / ExecStopPost; the hourly timer is opt-in (PLAN.md §9).
set -euo pipefail

PROFILE="${PROFILE:-/run/user/$(id -u)/room-display/profile}"
SNAP="${SNAP:-$HOME/.local/share/room-display/profile.tar.gz}"

# Whole profile minus caches. Auth is not just Cookies: Local Storage and
# IndexedDB hold session tokens too, so excluding by allowlist would silently
# log you out. Caches are disposable and are the bulk of the bytes.
EXCLUDES=(--exclude=Cache --exclude="Code Cache" --exclude=GPUCache
          --exclude=ShaderCache --exclude=GrShaderCache
          --exclude=CacheStorage --exclude=component_crx_cache)

case "${1:-}" in
restore)
  mkdir -p "$PROFILE"
  if [ ! -f "$SNAP" ]; then
    echo "no snapshot at $SNAP - starting with a fresh profile"
    exit 0
  fi
  # A truncated or corrupt archive must never stop the kiosk coming up: this Pi
  # has no keyboard, and a display stuck at a failed unit cannot be rescued from
  # the couch. Keep the bad one for post-mortem, carry on with a fresh profile.
  if ! tar -xzf "$SNAP" -C "$PROFILE"; then
    echo "snapshot unreadable - moving it to $SNAP.bad and starting fresh" >&2
    mv -f "$SNAP" "$SNAP.bad"
    rm -rf "$PROFILE"
    mkdir -p "$PROFILE"
    exit 0
  fi
  echo "restored $(du -sh "$PROFILE" | cut -f1) to $PROFILE"
  ;;

save)
  if [ ! -d "$PROFILE" ]; then
    echo "no profile at $PROFILE - nothing to save"
    exit 0
  fi
  # ponytail: 5-minute floor. Restart=always means a crash-looping agent fires
  # ExecStopPost every 5s, and without this that is an SD write every 5s -- the
  # exact wear this phase exists to stop. Drop it only if you also drop Restart.
  if [ -f "$SNAP" ] && [ -z "$(find "$SNAP" -mmin +5)" ]; then
    echo "snapshot is under 5 min old - skipping"
    exit 0
  fi

  mkdir -p "$(dirname "$SNAP")"
  # Before tar, not after: the archive holds session tokens, and a chmod that
  # follows creation leaves a window where it is world-readable (PLAN.md §10).
  umask 077
  rc=0
  # Write to .tmp and rename: mv is atomic, so a power cut mid-write leaves the
  # previous good snapshot intact instead of a truncated one.
  tar -C "$PROFILE" "${EXCLUDES[@]}" -czf "$SNAP.tmp" . || rc=$?
  # 1 = "file changed as we read it". Expected when the hourly timer catches a
  # live browser; the archive is still usable. 2 is a real failure.
  if [ "$rc" -gt 1 ]; then
    rm -f "$SNAP.tmp"
    exit "$rc"
  fi
  chmod 600 "$SNAP.tmp"          # session tokens (PLAN.md §10)
  mv -f "$SNAP.tmp" "$SNAP"
  echo "saved $(du -h "$SNAP" | cut -f1) to $SNAP"
  ;;

*)
  echo "usage: ${0##*/} restore|save" >&2
  exit 2
  ;;
esac
