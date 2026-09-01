#!/usr/bin/env bash
# Release-gated auto-update. Phase 8, PLAN.md §8.
#
# The Pi pulls; GitHub never reaches in. Runs from room-display-update.timer
# every ~30 min, deploys only tags you cut, and writes nothing when there is no
# new tag -- which is what keeps it off the SD card the other 47 times a day.
#
# The whole design exists for one reason: this Pi has no keyboard. A release
# that boots but breaks at runtime must undo itself, so the swap is gated on a
# boot check *and* verified against the live port afterwards, with a rollback.
set -euo pipefail

ROOT="${ROOT:-/opt/room-display}"
CACHE="$ROOT/cache-repo"
RELEASES="$ROOT/releases"
CURRENT="$ROOT/current"
CFG="${CFG:-/etc/room-display/config.toml}"
UNIT="${UNIT:-display-agent}"
PORT="${PORT:-8080}"
KEEP="${KEEP:-3}"
VERIFY_SECS="${VERIFY_SECS:-30}"

# The repo url, in order of preference: explicit, the cache clone, the Phase 5
# checkout. Public https needs no credential; for a private repo put an SSH url
# here and a read-only deploy key on the Pi (PLAN.md §8 "Auth").
REPO="${REPO:-}"
[ -n "$REPO" ] || REPO="$(git -C "$CACHE" remote get-url origin 2>/dev/null || true)"
[ -n "$REPO" ] || REPO="$(git -C "$CURRENT" remote get-url origin 2>/dev/null || true)"
[ -n "$REPO" ] || { echo "no repo url: set REPO=..." >&2; exit 2; }

# --- 1. cheap check: read-only, no writes, no clone ------------------------
TAG="$(git ls-remote --tags --refs "$REPO" 'v*' | sed 's|.*refs/tags/||' | sort -V | tail -1)"
[ -n "$TAG" ] || { echo "no v* tags on $REPO yet"; exit 0; }

RUNNING="$(basename "$(readlink -f "$CURRENT" 2>/dev/null || echo none)")"
if [ "$TAG" = "$RUNNING" ]; then
  echo "up to date ($TAG)"
  exit 0
fi
# A tag that failed the live verify will fail it again: same code, same box.
# Without this latch the timer redeploys it every 30 min forever, restarting the
# kiosk twice a cycle on a box with no keyboard -- the exact failure this file
# exists to prevent. Delete the marker to retry a tag after fixing the cause.
if [ -f "$RELEASES/.failed-$TAG" ]; then
  echo "$TAG failed verify before - skipping (rm $RELEASES/.failed-$TAG to retry)"
  exit 0
fi
echo "new release $TAG (running: $RUNNING)"

# --- 2. fetch the tag into its own release dir -----------------------------
[ -d "$CACHE" ] || git clone --bare "$REPO" "$CACHE"
git -C "$CACHE" fetch --prune --force origin '+refs/tags/*:refs/tags/*'

DEST="$RELEASES/$TAG"
rm -rf "$DEST"                      # a half-built dir from a killed run
mkdir -p "$DEST"
# archive, not clone: no per-release .git, so a release is code and nothing else.
git -C "$CACHE" archive "$TAG" | tar -x -C "$DEST"

# --- 3. build ---------------------------------------------------------------
python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install -q -r "$DEST/agent/requirements.txt"
echo "ROOM_VERSION=$TAG" > "$DEST/VERSION"     # display-agent.service reads this

# --- 4. boot check, gating the swap ----------------------------------------
# In-process, no port bound, no browser: safe while the live kiosk is up.
if ! (cd "$DEST" && ROOM_CONFIG="$CFG" .venv/bin/python -m agent selfcheck); then
  echo "selfcheck failed for $TAG - keeping $RUNNING" >&2
  exit 1
fi

# --- 5. swap, atomically ----------------------------------------------------
PREV="$(readlink "$CURRENT" 2>/dev/null || true)"
if [ -e "$CURRENT" ] && [ ! -L "$CURRENT" ]; then
  # Phase 5 left `current` as a real directory. ln -sfn onto a directory nests
  # the link *inside* it, so move it aside first -- and keep it, because until
  # this release proves itself it is the only thing we can roll back to.
  PREV="$ROOT/pre-phase8-$(date +%Y%m%d%H%M%S)"
  mv "$CURRENT" "$PREV"
fi
ln -sfn "$DEST" "$CURRENT"
systemctl --user restart "$UNIT"

# --- 6. verify against the live port ---------------------------------------
# This is the check selfcheck cannot do: a real restart, real browser, real
# socket. Runtime and kiosk regressions only ever show up here.
TOKEN="$(sed -n 's|^token = "\(.*\)"|\1|p' "$CFG")"
URL="http://$(tailscale ip -4 | head -1):$PORT/v1/status"
healthy=0
for _ in $(seq "$VERIFY_SECS"); do
  if curl -fsS -m 3 -H "Authorization: Bearer $TOKEN" "$URL" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 1
done

# --- 7. rollback ------------------------------------------------------------
if [ "$healthy" != 1 ]; then
  echo "ROLLBACK: $TAG did not answer $URL within ${VERIFY_SECS}s" >&2
  touch "$RELEASES/.failed-$TAG"      # latch, so the timer stops re-trying it
  if [ -n "$PREV" ] && [ -e "$PREV" ]; then
    ln -sfn "$PREV" "$CURRENT"
    systemctl --user restart "$UNIT"
    echo "ROLLBACK: restored $(basename "$PREV"). $TAG is left in $DEST for inspection." >&2
  else
    # Nothing to go back to. Say so loudly rather than pretending it worked --
    # this is the case where someone has to walk into the room.
    echo "ROLLBACK IMPOSSIBLE: no previous release recorded. Pi is on a broken $TAG." >&2
  fi
  exit 1
fi

echo "deployed $TAG"

# --- 8. prune, keeping the running and previous ones no matter what --------
KEEPERS="$TAG $(basename "${PREV:-none}")"
for d in $(ls -1 "$RELEASES" 2>/dev/null | sort -V | head -n "-$KEEP"); do
  case " $KEEPERS " in *" $d "*) continue ;; esac
  echo "pruning $d"
  rm -rf "${RELEASES:?}/$d"
done
