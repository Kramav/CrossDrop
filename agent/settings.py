"""The settings the agent owns and may rewrite at runtime.

`/etc/room-display/config.toml` is `root:<user> 640` because it holds the bearer
token, so the agent must never be able to write it (PLAN.md §7). The knobs that
are genuinely *runtime* rather than install-time live here instead: their own
file, in the agent's data dir, which survives `update.sh` replacing
`releases/<tag>/`.

JSON, not TOML: `tomllib` only reads, and a TOML writer is a new dependency for
a file that is written by the agent and edited through the web UI, never by hand.
"""

import json
import os
from pathlib import Path

# What the web UI may change. Everything else in config.toml stays file-only:
# the token, `profile_dir`, `upload.dir`, `debug_port` and `browser.kind` are
# install-time facts wired to the tmpfs layout, and changing them needs a
# browser relaunch, not a config reload. Display sleep timeouts and upload caps
# would be safe to add here — they were considered and left out of scope.
SCREEN_FIELDS = ("name", "home_url", "position", "size")


def path() -> Path:
    return Path(os.getenv("ROOM_SETTINGS")
                or Path.home() / ".local/share/room-display/settings.json")


def load() -> dict:
    """Saved overrides, or `{}`.

    Never raises. A missing or corrupt settings file must cost you your
    overrides, not the agent — config.toml on its own is always a valid boot,
    and on a box with no keyboard that is the difference between a bad edit and
    a drive home.
    """
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)        # atomic: a half-written file must not survive a crash


def apply(cfg: dict, data: dict | None = None) -> dict:
    """Overlay saved settings onto a freshly loaded config, in place.

    Screens match by **index**, not by name, because the name is itself editable
    — matching on it would make every rename look like a brand new screen and
    silently drop the override. Detection order is stable (`display.detect()`
    sorts left to right), so the index is the identity.
    """
    data = load() if data is None else data
    for screen, over in zip(cfg["screens"], data.get("screens") or []):
        for f in SCREEN_FIELDS:
            # Truthy, not `is not None`: blanking a field in the UI must fall
            # back to what xrandr detected rather than store an empty position
            # and leave a window unplaceable. That is the whole re-detect path.
            if over.get(f):
                screen[f] = over[f]
    return cfg
