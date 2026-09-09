#!/usr/bin/env bash
# Release-gated auto-update. Phase 8, PLAN.md §8.
#
# The Pi pulls; GitHub never reaches in. Runs from crossdrop-update.timer
# every ~30 min, deploys only tags you cut, and writes nothing when there is no
# new tag -- which is what keeps it off the SD card the other 47 times a day.
#
# The whole design exists for one reason: this Pi has no keyboard. A release
# that boots but breaks at runtime must undo itself, so the swap is gated on a
# boot check *and* verified against the live port afterwards, with a rollback.
set -euo pipefail

ROOT="${ROOT:-/opt/crossdrop}"
CACHE="$ROOT/cache-repo"
RELEASES="$ROOT/releases"
CURRENT="$ROOT/current"
CFG="${CFG:-/etc/crossdrop/config.toml}"
UNIT="${UNIT:-crossdrop-agent}"
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
# `|| true`: pipefail takes git's status, so an unreachable or private repo
# killed this before the "no v* tags" message below could explain it.
TAG="$(git ls-remote --tags --refs "$REPO" 'v*' 2>/dev/null | sed 's|.*refs/tags/||' | sort -V | tail -1 || true)"
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

# Opt-in signature check. Off by default, because turning it on without a
# signing key in place would stop every Pi updating at the next tag -- and a
# display stuck on an old release is a worse first experience than the risk it
# removes. On with VERIFY_TAG=1, and then it is a hard gate: an unsigned or
# badly signed tag never reaches the swap.
#
# What it buys, when you want it: without this, push access to the repo is code
# execution on every Pi within 30 minutes (PLAN.md §11 finding 10). Worth
# enabling on a box that matters, along with the signer's public key in the
# updating user's GnuPG keyring.
if [ "${VERIFY_TAG:-0}" = 1 ]; then
  if ! git -C "$CACHE" verify-tag "$TAG" 2>&1; then
    echo "VERIFY_TAG=1 and $TAG is not a validly signed tag - refusing" >&2
    # mkdir first: on a first-ever run nothing has created $RELEASES yet, and
    # under `set -e` a failed touch would kill the script with an error about
    # the wrong thing entirely.
    mkdir -p "$RELEASES"
    touch "$RELEASES/.failed-$TAG"     # latch, or the timer retries every 30 min
    exit 1
  fi
  echo "$TAG signature ok"
fi

DEST="$RELEASES/$TAG"
rm -rf "$DEST"                      # a half-built dir from a killed run
mkdir -p "$DEST"
# archive, not clone: no per-release .git, so a release is code and nothing else.
git -C "$CACHE" archive "$TAG" | tar -x -C "$DEST"

# --- 3. build ---------------------------------------------------------------
python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install -q --require-hashes -r "$DEST/agent/requirements.txt"
echo "CROSSDROP_VERSION=$TAG" > "$DEST/VERSION"     # crossdrop-agent.service reads this

# --- 4. boot check, gating the swap ----------------------------------------
# In-process, no port bound, no browser: safe while the live kiosk is up.
if ! (cd "$DEST" && CROSSDROP_CONFIG="$CFG" .venv/bin/python -m agent selfcheck); then
  echo "selfcheck failed for $TAG - keeping $RUNNING" >&2
  # Latch, like every other refusal here. Without it `current` never becomes
  # $TAG, so the timer repeats the whole build -- rm -rf, git archive, a fresh
  # venv and a 30-package pip install -- 48 times a day, forever, onto the SD
  # card that journald-in-RAM exists to spare.
  #
  # Note what this cannot reach: a box still on the v1 layout runs the *v1*
  # update.sh (its unit's ExecStart is frozen at the old path), and that one has
  # no latch here -- so an un-migrated Pi with the update timer on rebuilds a
  # venv every 30 minutes until it is migrated. That is a reason to migrate, not
  # something this file can fix retroactively. See README "Upgrading from v1.x".
  mkdir -p "$RELEASES"
  touch "$RELEASES/.failed-$TAG"
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
# Guarded, and the failure is not fatal here. Unguarded under `set -e` this
# ended the script one line after the swap: no verify, no /v1/inspect check, no
# rollback, no latch, no message -- and the next run reads `current` as $TAG and
# reports "up to date", so a broken release became permanent silently. Reached
# by running this by hand over SSH with no session bus, which is the drill
# smoke-on-the-pi.md prescribes. Falling through leaves `restarted` false, and
# the verify below then treats it exactly as a release that would not come up.
restarted=1
systemctl --user restart "$UNIT" || restarted=0

# --- 6. verify against the live port ---------------------------------------
# This is the check selfcheck cannot do: a real restart, real browser, real
# socket. Runtime and kiosk regressions only ever show up here.
# head -1: a second matching line -- a commented-out old token, a [section] that
# also has one -- would make TOKEN multi-line, the Authorization header
# malformed, every health probe 401, and the release roll back for no reason.
# A *false* rollback on a box with no keyboard is the expensive failure here.
#
# Both `|| true`: `set -o pipefail` makes each of these take the *first*
# command's status, and `set -e` then ends the script -- here, after the symlink
# has been swapped and the agent restarted, so there is no verify, no
# /v1/inspect check, no rollback and no `.failed-$TAG` latch. The next timer run
# sees RUNNING == TAG and reports "up to date", so a broken release becomes
# permanent silently. Reached whenever tailscaled is restarting, the node is
# logged out, or its key has expired -- which the installer's own closing banner
# warns about. Empty is handled explicitly below.
TOKEN="$(sed -n 's|^token = "\(.*\)"|\1|p' "$CFG" | head -1 || true)"
TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
if [ "$restarted" = 0 ]; then
  echo "could not restart $UNIT (no session bus?) - rolling back" >&2
  healthy=0
  latch=0        # the release is untried, not proven bad; see below
  BASE=""
elif [ -z "$TS_IP" ] || [ -z "$TOKEN" ]; then
  # Cannot verify, so do not keep an unverified release. Roll back and latch,
  # exactly as a failed verify would -- "we could not check" and "the check
  # failed" deserve the same answer once the swap has already happened.
  if [ -z "$TS_IP" ]; then why="no tailscale address"; else why="no token in $CFG"; fi
  echo "cannot verify $TAG: $why - rolling back" >&2
  healthy=0
  # Roll back, but do NOT latch. The latch means "this tag fails on this box,
  # do not waste a restart on it again", and that is a claim about the *code*.
  # tailscaled restarting, an expired node key or an unreadable config says
  # nothing about the release -- and latching on it pins the Pi off a good tag
  # for good, undoable only by deleting a marker on a box that, if the cause
  # was tailscale, nobody can currently reach.
  latch=0
  BASE=""
else
  healthy=0
  BASE="http://$TS_IP:$PORT"
fi
URL="$BASE/v1/status"
for _ in $(seq "$([ -n "$BASE" ] && echo "$VERIFY_SECS" || echo 0)"); do
  if curl -fsS -m 3 -H "Authorization: Bearer $TOKEN" "$URL" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 1
done

# --- 6b. and against the wall ----------------------------------------------
# /v1/status answering proves the agent is up, not that anything is on the
# monitors: Chromium's own error page renders perfectly and returns a happy 200
# through every route above. /v1/inspect is the assertion that sees it.
#
# Lenient on purpose. A release is rolled back only if the kiosk *reports* an
# error page. inspect not answering at all -- an older agent, a firefox box, a
# browser still coming up -- is "cannot tell", and a false rollback on a box
# with no keyboard is worse than the regression it would be guarding against.
page_ok=1
if [ "$healthy" = 1 ]; then
  for _ in $(seq 15); do
    body="$(curl -fsS -m 3 -H "Authorization: Bearer $TOKEN" "$BASE/v1/inspect" 2>/dev/null || true)"
    case "$body" in
      *'"error_page":false'*) page_ok=1; break ;;
      *'"error_page":true'*)  page_ok=0 ;;
      *) break ;;                     # 501, 404, no answer: nothing to judge
    esac
    sleep 1
  done
  [ "$page_ok" = 1 ] || echo "kiosk is showing a browser error page" >&2
fi

# What the wall looked like when it went wrong, for whoever reads this later.
# Diagnostic, never a gate: no pixel heuristic is worth a false rollback here.
# Best-effort, because a release broken enough to fail the checks above may not
# manage a screenshot either.
#
# jpeg to bound the worst case on an SD card, not because it is always smaller:
# on a flat error page png usually wins, but a full-screen photo runs to
# megabytes as png and a few hundred KB as jpeg. Bounding is the point here.
snapshot_failure() {
  local out="$RELEASES/.failed-$TAG.jpg"
  curl -fsS -m 10 -X POST -H "Authorization: Bearer $TOKEN" \
       -H 'Content-Type: application/json' -d '{"format":"jpeg","quality":60}' \
       "$BASE/v1/screenshot" 2>/dev/null \
    | sed -n 's/.*"image":"\([^"]*\)".*/\1/p' | base64 -d > "$out" 2>/dev/null || true
  if [ -s "$out" ]; then
    echo "what the wall was showing: $out" >&2
  else
    rm -f "$out"
  fi
}

# --- 7. rollback ------------------------------------------------------------
if [ "$healthy" != 1 ] || [ "$page_ok" != 1 ]; then
  snapshot_failure
  if [ -n "$BASE" ]; then
    echo "ROLLBACK: $TAG did not come up healthy at $BASE within ${VERIFY_SECS}s" >&2
  else
    echo "ROLLBACK: $TAG could not be checked at all, so it is not being kept" >&2
  fi
  # Latch only when the release itself was tried and found wanting. An
  # environmental failure gets a rollback and a retry next cycle.
  if [ "${latch:-1}" = 1 ]; then
    touch "$RELEASES/.failed-$TAG"    # latch, so the timer stops re-trying it
  else
    echo "not latching $TAG: the check could not run, so the release is" >&2
    echo "untried rather than known-bad. It will be retried next cycle." >&2
  fi
  if [ -n "$PREV" ] && [ -e "$PREV" ]; then
    ln -sfn "$PREV" "$CURRENT"
    # Guarded: one of the two ways to get here is that this very command just
    # failed. Unguarded, `set -e` ended the run before the line below could say
    # the symlink had been put back -- the box was correct and the log did not
    # say so, which on a Pi you cannot reach is the whole story you have.
    systemctl --user restart "$UNIT"       || echo "ROLLBACK: could not restart $UNIT; the symlink is back, so the"               "next boot or the 04:00 timer will pick up $(basename "$PREV")." >&2
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
# Markers for tags that no longer have a release directory are just litter, and
# `ls -1` below never sees them because they are dotfiles.
for m in "$RELEASES"/.failed-*; do
  case "$m" in *'*'*) continue ;; esac
  t="$(basename "$m")"; t="${t#.failed-}"; t="${t%.jpg}"
  [ -e "$RELEASES/$t" ] || rm -f "$m"
done
# The pre-Phase-8 checkout, which every migrated box passes through, has its own
# .venv and was never pruned by the loop below -- it is not under $RELEASES.
for old in "$ROOT"/pre-phase8-*; do
  case "$old" in *'*'*) continue ;; esac
  [ "$old" = "${PREV:-}" ] && continue
  echo "pruning $(basename "$old")"
  rm -rf "$old"
done

KEEPERS="$TAG $(basename "${PREV:-none}")"
for d in $(ls -1 "$RELEASES" 2>/dev/null | sort -V | head -n "-$KEEP"); do
  case " $KEEPERS " in *" $d "*) continue ;; esac
  echo "pruning $d"
  rm -rf "${RELEASES:?}/$d"
done
