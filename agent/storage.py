"""Upload store for dropped reference files.

On the Pi this directory is tmpfs, so it is RAM: every file here costs memory
until reboot. Hence the size cap and the keep-newest-N sweep.
"""

import contextlib
import re
import secrets
from pathlib import Path

# Extension -> the type we serve it as; the client's Content-Type is never
# echoed. No SVG: it is a script-bearing document and /files is unauthenticated
# (app.py's CSP sandbox is the structural half of the same defence).
TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".txt": "text/plain; charset=utf-8",
    # Clips, not films: tmpfs, so an upload is RAM until the sweep. Raise
    # upload.max_mb knowing that; push long video as a URL instead.
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
}

ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,}\.[a-z0-9]{1,5}$")


class TooBig(Exception):
    pass


class BadType(Exception):
    pass


def save(cfg: dict, filename: str, chunks) -> str:
    """Stream `chunks` to the store, return the id. Raises TooBig / BadType."""
    up = cfg["upload"]
    # The client's filename is never a path — only its extension, and only from
    # the allowlist. That is the whole of filename sanitising.
    ext = Path(filename or "").suffix.lower()
    if ext not in TYPES:
        raise BadType(f"{ext or filename!r} not allowed; try {', '.join(sorted(TYPES))}")

    d = Path(up["dir"])
    d.mkdir(parents=True, exist_ok=True)
    file_id = secrets.token_urlsafe(12) + ext
    dest, cap, written = d / file_id, up["max_mb"] * 1024 * 1024, 0

    # Before the write as well as after: a full tmpfs makes the write ENOSPC,
    # and a sweep that only ran after a *successful* write would never free the
    # space again -- one full tmpfs wedging uploads until someone SSHes in.
    sweep(cfg)

    try:
        with dest.open("wb") as f:
            for chunk in chunks:
                written += len(chunk)
                if written > cap:
                    raise TooBig(f"over {up['max_mb']} MB")
                f.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)  # never leave a partial file in RAM
        raise

    # Never the one we just wrote: its url is about to go to the kiosk.
    sweep(cfg, spare=file_id)
    return file_id


def path(cfg: dict, file_id: str) -> Path:
    """Resolve an id to a file. Raises KeyError if it is not a real stored id."""
    if not ID_RE.match(file_id):
        raise KeyError(file_id)  # no separators, no dots, no traversal
    p = Path(cfg["upload"]["dir"]) / file_id
    if not p.is_file():
        raise KeyError(file_id)
    return p


def media_type(file_id: str) -> str:
    return TYPES[Path(file_id).suffix.lower()]


def sweep(cfg: dict, spare: str | None = None) -> None:
    """Keep the newest N uploads, drop the rest. `spare` is never dropped.

    ponytail: crude, but this is RAM on a box nobody logs into — an age- or
    byte-budget policy if that ever bites.

    Three things that each cost a 404 on the wall for a file that uploaded
    perfectly, so none of them is incidental:

      - `keep` is floored at 1. `files[:-0 or None]` is `files[:None]` — every
        file, including the one the kiosk is about to fetch.
      - `spare` is named, not inferred from a clock. Mtime says which file is
        newest, not which one somebody is waiting for.
      - a file that vanishes between the glob and the stat is skipped. Two
        uploads sweep concurrently, and save() re-raises this as a 500.
    """
    keep = max(1, cfg["upload"]["keep"])
    files = []
    for p in Path(cfg["upload"]["dir"]).glob("*"):
        with contextlib.suppress(OSError):          # swept by a concurrent upload
            files.append((p.stat().st_mtime, p))
    files.sort(key=lambda t: t[0])
    for _, old in files[:-keep]:
        if old.name != spare:
            old.unlink(missing_ok=True)
