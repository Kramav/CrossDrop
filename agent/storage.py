"""Upload store for dropped reference files.

On the Pi this directory is tmpfs, so it is RAM: every file here costs memory
until reboot. Hence the size cap and the keep-newest-N sweep.
"""

import re
import secrets
from pathlib import Path

# Extension -> the type we will serve it as. We never echo the client's
# Content-Type; a .pdf full of HTML must still reach the browser as a PDF.
# No SVG: it is a script-bearing document, and /files is unauthenticated.
TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".txt": "text/plain; charset=utf-8",
}

ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,}\.[a-z0-9]{1,5}$")


class TooBig(Exception):
    pass


class BadType(Exception):
    pass


def save(cfg: dict, filename: str, chunks) -> str:
    """Stream `chunks` to the store, return the id. Raises TooBig / BadType."""
    up = cfg["upload"]
    # The client's filename is never used as a path — only its extension, and
    # only if it is on the allowlist. That is the whole of filename sanitising.
    ext = Path(filename or "").suffix.lower()
    if ext not in TYPES:
        raise BadType(f"{ext or filename!r} not allowed; try {', '.join(sorted(TYPES))}")

    d = Path(up["dir"])
    d.mkdir(parents=True, exist_ok=True)
    file_id = secrets.token_urlsafe(12) + ext
    dest, cap, written = d / file_id, up["max_mb"] * 1024 * 1024, 0

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

    sweep(cfg)
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


def sweep(cfg: dict) -> None:
    # ponytail: keep the newest N, drop the rest. Crude, but this is RAM on a
    # box nobody logs into — an age- or byte-budget policy if that ever bites.
    files = sorted(Path(cfg["upload"]["dir"]).glob("*"), key=lambda p: p.stat().st_mtime)
    for old in files[: -cfg["upload"]["keep"] or None]:
        old.unlink(missing_ok=True)
