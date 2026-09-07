"""Unpacked browser extensions for the kiosk.

Owns `browser.extensions_dir` the way storage.py owns the upload directory --
on the Pi this one is on the SD card, not tmpfs, because an extension has to
survive the boot that the profile does not.

Unpacked rather than the Web Store because the Debian build the Pi runs ignores
ExtensionInstallForcelist (deploy/pi/README.md §10), so we fetch the CRX and
hand Chromium a directory. Nothing here loads anything: `--load-extension` is a
launch flag, so an install takes effect at the next browser start -- pending().
"""

import io
import json
import re
import shutil
import urllib.request
import zipfile
from pathlib import Path

# Ids only, never a url, interpolated into a fixed template: this downloads and
# unpacks code onto the box, and a caller-supplied url would make it a
# general-purpose fetcher (PLAN.md §11).
STORE = ("https://clients2.google.com/service/update2/crx"
         "?response=redirect&acceptformat=crx3&prodversion=130&x=id%3D{id}%26uc")
# Exactly 32 characters of a-p, checked before anything is fetched -- so a typo
# is an error, not an HTML page unpacked as "the extension".
ID_RE = re.compile(r"^[a-p]{32}$")
MAX_MB = 50
TIMEOUT = 60.0


class BadId(Exception):
    pass


class TooBig(Exception):
    pass


def scan(dir: str) -> list[str]:
    """Installed extension directories, in a stable order.

    A child without a manifest.json is skipped, not handed to Chromium: an
    interrupted unpack would take the whole kiosk down at launch, and a display
    that will not start beats no ad blocker. Same for a missing directory.
    """
    if not dir or not Path(dir).is_dir():
        return []
    return sorted(str(p) for p in Path(dir).iterdir()
                  if (p / "manifest.json").is_file())


def display_name(path: str | Path) -> str:
    """The extension's own name, for a UI that would otherwise list 32-char ids.

    Translated manifests put a placeholder in `name` and the real string in
    _locales -- uBlock Origin Lite is one -- so resolving it is the difference
    between a readable list and a page of hashes. Anything failing falls back to
    the directory name: a missing label must not fail a route that worked.
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

    The directory is named for the id: stable across renames, cannot collide,
    and reinstalling is an in-place replacement rather than a second copy.
    """
    if not ID_RE.match(id or ""):
        raise BadId(f"not an extension id: {id!r}")
    if not dir:
        raise BadId("no extensions_dir configured")

    cap = int(MAX_MB * 1024 * 1024)
    with _open(STORE.format(id=id), timeout=TIMEOUT) as r:
        # read(cap + 1), not read(): the sender must not size the response.
        # The extra byte tells "at the cap" from "over it".
        blob = r.read(cap + 1)
    if len(blob) > cap:
        raise TooBig(f"over {MAX_MB} MB")

    root = Path(dir)
    root.mkdir(parents=True, exist_ok=True)
    staged = root / f"{id}.new"
    shutil.rmtree(staged, ignore_errors=True)
    # A CRX3 is a header followed by a zip, and zipfile finds the end-of-central
    # -directory by scanning back from the end -- so it reads an archive with
    # junk in front of it, with no unzip binary and no subprocess. extractall
    # sanitises member paths, so a hostile CRX cannot write outside `staged`.
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        # The download cap bounds the *compressed* bytes; a zip bomb is 50 MB in
        # and gigabytes out, onto the SD card this design exists to spare.
        # ponytail: file_size is the archive's own claim, so this bounds the
        # honest-but-huge case. Meter the extract stream if a hostile CRX is
        # ever in scope.
        if sum(i.file_size for i in z.infolist()) > cap:
            raise TooBig(f"{id}: unpacks to over {MAX_MB} MB")
        z.extractall(staged)

    if not (staged / "manifest.json").is_file():
        shutil.rmtree(staged, ignore_errors=True)
        raise ValueError(
            f"{id}: no manifest.json at the top level -- not an extension")

    # Swap whole: Chromium refuses a half-replaced directory at the next
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
    autolaunch = false `loaded` is empty and everything reads as pending, which
    is honest: we did not start that browser and cannot say what it loaded.
    """
    return set(scan(dir)) != set(loaded)
