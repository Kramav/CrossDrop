"""Unpacked browser extensions for the kiosk.

Owns `browser.extensions_dir` the way storage.py owns the upload directory. On
the Pi this one is on the SD card, not tmpfs: an extension has to survive the
boot that the profile does not.

Why unpacked rather than the Web Store: Chromium's own ExtensionInstallForcelist
policy is ignored by the Debian build the Pi runs, though the store itself is
reachable (deploy/pi/README.md §10). So we fetch the CRX ourselves and hand
Chromium a directory.

Nothing here loads anything. `--load-extension` is a launch flag, so an install
takes effect at the next browser start -- see pending().
"""

import io
import json
import re
import shutil
import urllib.request
import zipfile
from pathlib import Path

# The id is interpolated into a fixed template and the request body carries ids
# only, never a url. That is deliberate: this route downloads and unpacks code
# onto the box, and a caller-supplied url would make it a general-purpose
# fetcher (PLAN.md §11).
STORE = ("https://clients2.google.com/service/update2/crx"
         "?response=redirect&acceptformat=crx3&prodversion=130&x=id%3D{id}%26uc")
# Store ids are exactly 32 characters of a-p. Checked before anything is
# fetched, so a typo is an error rather than an HTML page unpacked as "the
# extension".
ID_RE = re.compile(r"^[a-p]{32}$")
MAX_MB = 50
TIMEOUT = 60.0


class BadId(Exception):
    pass


class TooBig(Exception):
    pass


def scan(dir: str) -> list[str]:
    """Installed extension directories, in a stable order.

    A child without a manifest.json is skipped rather than passed to Chromium:
    an interrupted unpack would otherwise take the whole kiosk down at launch,
    and a display that will not start is a worse failure than a missing ad
    blocker. Same reason a missing directory is simply "no extensions".
    """
    if not dir or not Path(dir).is_dir():
        return []
    return sorted(str(p) for p in Path(dir).iterdir()
                  if (p / "manifest.json").is_file())


def display_name(path: str | Path) -> str:
    """The extension's own name, for a UI that would otherwise list 32-char ids.

    Manifests written for translation put a placeholder in `name` and the real
    string in _locales -- uBlock Origin Lite is one of them, so resolving it is
    the difference between a readable list and a page of hashes. Any of this
    failing falls back to the directory name; a missing label must never be an
    error on a route that otherwise worked.
    """
    p = Path(path)
    try:
        manifest = json.loads((p / "manifest.json").read_text(encoding="utf-8"))
        name = manifest.get("name", "")
        if name.startswith("__MSG_") and name.endswith("__"):
            key = name[6:-2]
            messages = json.loads(
                (p / "_locales" / manifest.get("default_locale", "en")
                 / "messages.json").read_text(encoding="utf-8"))
            name = messages[key]["message"]
        return name or p.name
    except (OSError, ValueError, KeyError, AttributeError):
        return p.name


def install(dir: str, id: str, _open=urllib.request.urlopen) -> str:
    """Download extension `id` from the Web Store into `dir`. Returns its name.

    The directory is named for the id, not for the extension: it is stable
    across renames, cannot collide, and makes reinstalling the same id an
    in-place replacement rather than a second copy.
    """
    if not ID_RE.match(id or ""):
        raise BadId(f"not an extension id: {id!r}")
    if not dir:
        raise BadId("no extensions_dir configured")

    cap = int(MAX_MB * 1024 * 1024)
    with _open(STORE.format(id=id), timeout=TIMEOUT) as r:
        # read(cap + 1) rather than read(): a hostile or broken response must not
        # be sized by the sender. One extra byte is how we tell "at the cap" from
        # "over it".
        blob = r.read(cap + 1)
    if len(blob) > cap:
        raise TooBig(f"over {MAX_MB} MB")

    root = Path(dir)
    root.mkdir(parents=True, exist_ok=True)
    staged = root / f"{id}.new"
    shutil.rmtree(staged, ignore_errors=True)
    # A CRX3 is a header followed by a zip. zipfile finds the end-of-central-
    # directory by scanning back from the end and offsets the entries
    # accordingly, so it reads an archive with junk in front of it -- which is
    # why this needs no unzip binary and no subprocess.
    # extractall sanitises member paths (leading slashes and .. are stripped),
    # so a hostile CRX cannot write outside `staged`.
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        z.extractall(staged)

    if not (staged / "manifest.json").is_file():
        shutil.rmtree(staged, ignore_errors=True)
        raise ValueError(
            f"{id}: no manifest.json at the top level -- not an extension")

    # Swap whole. A half-replaced directory is one Chromium refuses at the next
    # launch, and that launch is the kiosk coming up.
    dest = root / id
    shutil.rmtree(dest, ignore_errors=True)
    staged.rename(dest)
    return display_name(dest)


def remove(dir: str, name: str) -> None:
    """Delete one installed extension. Raises KeyError if it is not installed."""
    # `name` comes off the wire, so it is resolved against the scan rather than
    # joined onto the path -- ".." never reaches the filesystem.
    for p in scan(dir):
        if Path(p).name == name:
            shutil.rmtree(p)
            return
    raise KeyError(name)


def pending(dir: str, loaded: list[str]) -> bool:
    """True when what is on disk is not what the running browser was given.

    Covers removals and anything installed over SSH, not just this API. With
    autolaunch = false `loaded` is empty and any installed extension reads as
    pending, which is honest: we did not start that browser and cannot say what
    it loaded.
    """
    return set(scan(dir)) != set(loaded)
