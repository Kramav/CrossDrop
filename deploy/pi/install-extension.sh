#!/usr/bin/env bash
# Install a Chrome Web Store extension into the kiosk. See README.md §10.
#
#   install-extension.sh <id-or-store-url> [name]
#   install-extension.sh ddkjiahejlhfcafbddmgiahcphecmpfh ublock-lite
#
# Why not ExtensionInstallForcelist: Debian's Chromium ignores it (README §10),
# so the extension has to arrive as an unpacked directory. The agent loads every
# child of extensions_dir that has a manifest.json, which is what keeps this
# script to "download, unpack, restart" with no config edit and no root -- setup.sh
# already gives $USER /opt/room-display.
set -euo pipefail

DEST_DIR="${EXTENSIONS_DIR:-/opt/room-display/extensions}"
UNIT=display-agent.service

[ $# -ge 1 ] || { echo "usage: $(basename "$0") <id-or-store-url> [name]"; exit 1; }
command -v unzip >/dev/null || { echo "need unzip: sudo apt install -y unzip"; exit 1; }

# Accept a store URL as well as a bare id -- the id is what you can copy off the
# address bar, the URL is what you can copy off the page.
ID="${1##*/}"
ID="${ID%%\?*}"
# Store ids are exactly 32 characters of a-p. Catching a typo here is the
# difference between an error and a downloaded HTML page unpacked as "the
# extension".
printf '%s' "$ID" | grep -qE '^[a-p]{32}$' || { echo "not an extension id: $ID"; exit 1; }
NAME="${2:-$ID}"

# The store picks which build to serve from prodversion, so ask the browser we
# are actually installing for rather than pinning a number that ages.
PROD=$(chromium --version 2>/dev/null | grep -oE '[0-9]+' | head -1 || true)
URL="https://clients2.google.com/service/update2/crx?response=redirect&acceptformat=crx3&prodversion=${PROD:-130}&x=id%3D${ID}%26uc"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "== downloading $ID (prodversion ${PROD:-130})"
curl -fsSL -o "$TMP/ext.crx" "$URL"

# A CRX3 is a header followed by a zip, so unzip reads it and warns about the
# leading bytes. That warning is exit 1, hence the ||: -- the manifest check
# below is what actually decides whether the unpack worked.
echo "== unpacking"
unzip -q -o "$TMP/ext.crx" -d "$TMP/ext" || true
[ -f "$TMP/ext/manifest.json" ] || {
  echo "no manifest.json at the top level -- not an extension, or the download was HTML"
  exit 1
}

# Swap it in whole: a half-replaced extension directory is one Chromium would
# refuse at the next launch, and that launch is the kiosk coming up.
mkdir -p "$DEST_DIR"
rm -rf "${DEST_DIR:?}/$NAME.new"
mv "$TMP/ext" "$DEST_DIR/$NAME.new"
rm -rf "${DEST_DIR:?}/$NAME"
mv "$DEST_DIR/$NAME.new" "$DEST_DIR/$NAME"
echo "== installed $DEST_DIR/$NAME"

if systemctl --user is-active --quiet "$UNIT"; then
  echo "== restarting $UNIT"
  systemctl --user restart "$UNIT"
  sleep 10
  # An extension's service worker is a CDP target, so this needs no display.
  # Report by manifest name: an unpacked extension's id is derived from its path,
  # not from the store id, so neither $ID nor $NAME appears in the target list.
  if curl -s localhost:9222/json | python3 -c '
import json, sys
names = [t.get("title", "?") for t in json.load(sys.stdin)
         if t.get("url", "").startswith("chrome-extension://")]
print("== loaded:", ", ".join(names) if names else "nothing")
sys.exit(0 if names else 1)'; then
    :
  else
    echo "   Check extensions_dir in /etc/room-display/config.toml points at"
    echo "   $DEST_DIR, then: journalctl --user -u $UNIT | grep browser:"
    exit 1
  fi
else
  echo "== $UNIT is not running; start it to pick this up"
fi
