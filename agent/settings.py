"""The settings the agent owns and may rewrite at runtime.

config.toml is `root:<user> 640` because it holds the bearer token, so the agent
must never be able to write it (PLAN.md §7). Genuinely *runtime* knobs live here
instead, in the agent's data dir, which survives update.sh replacing
`releases/<tag>/`. JSON because `tomllib` only reads.
"""

import json
import os
from pathlib import Path

# What the web UI may change. The token, `profile_dir`, `upload.dir`,
# `debug_port` and `browser.kind` stay file-only: install-time facts wired to
# the tmpfs layout, needing a browser relaunch rather than a config reload.
SCREEN_FIELDS = ("name", "home_url", "position", "size")

# The deployed name, not the repo name -- PLAN.md §11 "Naming". Renaming an
# installation is a migration on hardware nobody can reach with a keyboard.
DATA_DIR = ".local/share/room-display"


def data_dir() -> Path:
    """settings.json, last.json, and the profile snapshot deploy/pi/
    profile-snapshot.sh writes beside them.

    ROOM_DATA makes moving these a deployment decision rather than a code edit,
    and profile-snapshot.sh reads the same variable — a systemd `Environment=`
    line covers ExecStartPre and ExecStopPost too, so the two halves cannot
    drift. Changing the literal above instead brings a box up with no saved
    screens and no restored page, which nobody notices until they look.
    """
    return Path(os.getenv("ROOM_DATA") or Path.home() / DATA_DIR)


def path() -> Path:
    # ROOM_SETTINGS names the file outright and still wins: it predates
    # ROOM_DATA and the tests point it at a tmp_path.
    return Path(os.getenv("ROOM_SETTINGS") or data_dir() / "settings.json")


def last_path() -> Path:
    """What each screen was showing when the agent last stopped. Its own file,
    not a key in settings.json: `PUT /v1/settings` rewrites that one whole."""
    return path().with_name("last.json")


def load(p: Path | None = None) -> dict:
    """Saved overrides, or `{}`. Never raises — a missing or corrupt settings
    file must cost you your overrides, not the agent, and config.toml on its own
    is always a valid boot."""
    try:
        data = json.loads((p or path()).read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError):
        # RuntimeError: Path.home() raises it with no resolvable home dir. A
        # user unit always has HOME, but this is on the path that decides
        # whether the agent boots at all, so it is not worth being clever about.
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict, p: Path | None = None) -> None:
    p = p or path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)        # atomic: a half-written file must not survive a crash


def merge_screens(rows: list[dict]) -> dict:
    """`rows` overlaid onto the saved screen list by index, ready for save().

    save() rewrites the file whole and the editor only ever sees the screens
    `display.detect()` found *now* — so a save with a monitor unplugged used to
    drop the other screen's name and home_url for good, and plugging it back in
    did not bring them back. Longer saved lists keep their tail instead.
    """
    saved = load().get("screens") or []
    return {"screens": [rows[i] if i < len(rows) else saved[i]
                        for i in range(max(len(rows), len(saved)))]}


def apply(cfg: dict, data: dict | None = None) -> dict:
    """Overlay saved settings onto a freshly loaded config, in place.

    By **index**, not by name: the name is itself editable, so matching on it
    would make every rename look like a new screen and drop the override.
    `display.detect()` sorts left to right, so the index is the identity.
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
