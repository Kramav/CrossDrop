"""The settings the agent owns and may rewrite at runtime.

config.toml is `root:<user> 640` because it holds the bearer token, so the agent
must never be able to write it (PLAN.md §7). Genuinely *runtime* knobs live here
instead, in the agent's data dir, which survives update.sh replacing
`releases/<tag>/`. JSON because `tomllib` only reads.
"""

import json
import logging
import os
from pathlib import Path

# What the web UI may change. The token, `profile_dir`, `upload.dir`,
# `debug_port` and `browser.kind` stay file-only: install-time facts wired to
# the tmpfs layout, needing a browser relaunch rather than a config reload.
#
# `position` and `size` are deliberately absent: they are facts display.detect()
# re-reads at every boot, and persisting a derived fact is what makes it go
# stale. A layout saved against one set of monitors used to win over the set
# actually attached -- swap a screen, or save while one is unplugged, and both
# kiosk windows landed on the same output with no way to tell why. xrandr is the
# only source of truth for geometry now; config.toml's [[screen]] blocks remain
# the way to pin one by hand, because that file is edited by somebody who had a
# keyboard.
SCREEN_FIELDS = ("name", "home_url")

# The partition, beside SCREEN_FIELDS: the non-screen settings the API may
# write. Deliberately short. A key earns a place here by being something the web
# UI actually changes -- not merely by being harmless.
#
# Everything else stays in config.toml, which is root-owned and which the agent
# cannot write: `browser.path` is what launch() execs, `extensions_dir` is where
# unpacked code is loaded from, `[interact]` is the typing switch, `[server]` is
# what the agent binds. Those decide *what runs* on the box.
#
# The install-time tuning -- home_url, the display timers, disk_cache_mb, the
# upload caps -- stays there too, for a duller reason: nothing edits it. Moving
# a key here that no caller writes would buy a second place to look and a
# precedence rule to remember, which is the confusion this partition exists to
# remove.
#
# (section, key); section None would mean the top level.
SAFE_KEYS = (
    ("browser", "mode"),        # kiosk / fullscreen, toggled from the UI
    ("display", "splits"),      # which outputs are cut in half, by connector
)

# The deployed name, not the repo name -- PLAN.md §11 "Naming". Renaming an
# installation is a migration on hardware nobody can reach with a keyboard.
DATA_DIR = ".local/share/crossdrop"

# A child of app.py's logger, so it lands in the same journal.
log = logging.getLogger("crossdrop.settings")


def data_dir() -> Path:
    """settings.json, last.json, and the profile snapshot deploy/pi/
    profile-snapshot.sh writes beside them.

    CROSSDROP_DATA makes moving these a deployment decision rather than a code edit,
    and profile-snapshot.sh reads the same variable — a systemd `Environment=`
    line covers ExecStartPre and ExecStopPost too, so the two halves cannot
    drift. Changing the literal above instead brings a box up with no saved
    screens and no restored page, which nobody notices until they look.
    """
    return Path(os.getenv("CROSSDROP_DATA") or Path.home() / DATA_DIR)


def path() -> Path:
    # CROSSDROP_SETTINGS names the file outright and still wins: it predates
    # CROSSDROP_DATA and the tests point it at a tmp_path.
    return Path(os.getenv("CROSSDROP_SETTINGS") or data_dir() / "settings.json")


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
    # Same shape guard as apply(): this indexes `saved`, so a `screens` that is
    # not a list raised out of PUT /v1/settings, and one holding non-objects
    # would write them straight back for the next boot to choke on.
    saved = load().get("screens")
    saved = [r for r in saved if isinstance(r, dict)] if isinstance(saved, list) else []
    return {"screens": [rows[i] if i < len(rows) else saved[i]
                        for i in range(max(len(rows), len(saved)))]}


def apply(cfg: dict, data: dict | None = None) -> dict:
    """Overlay saved settings onto a freshly loaded config, in place.

    Matched on `output` -- the connector a screen came from ("HDMI-1"), or one
    half of it ("HDMI-1-L") -- falling back to list index for rows saved before
    that field existed.

    Not by *name*: the name is editable, so matching on it would make every
    rename look like a new screen and drop the override. Index was the identity
    until splitting arrived; a split adds and removes rows, so every saved name
    after the split point would slide onto the wrong screen. `output` is the one
    thing here that neither the user nor the layout can change.
    """
    data = load() if data is None else data
    # Shape-checked, not just type-checked. `load()` only proved the top level is
    # a dict, so `{"screens": {"a": 1}}`, `{"screens": ["x"]}`, `{"screens": [null]}`
    # and `{"screens": 5}` all reached the loop below and raised AttributeError or
    # TypeError out of load_config -- which lifespan does not catch, so uvicorn
    # died and Restart=always looped it. This file outlives update.sh and the
    # nightly restart, so that crash loop was permanent on a box with no
    # keyboard. `_dedupe` guards the *names* in here; this guards the shape.
    rows = data.get("screens")
    if not isinstance(rows, list):
        if rows is not None:
            log.error("settings.json: `screens` is %s, not a list — ignoring the "
                      "saved overrides", type(rows).__name__)
        rows = []
    rows = [r for r in rows if _an_object(r)]
    # Rows that name an output are matched to the screen from that output; the
    # rest fall back to their old positional meaning. A file written before
    # `output` existed therefore behaves exactly as it did, and gets stamped on
    # the next save -- which is the whole migration.
    by_output = {r["output"]: r for r in rows if r.get("output")}
    positional = [r for r in rows if not r.get("output")]
    for screen, over in zip(cfg["screens"], positional):
        _overlay(screen, over)
    for screen in cfg["screens"]:
        over = by_output.get(screen.get("output"))
        if over:
            _overlay(screen, over)

    return cfg


def apply_keys(cfg: dict, data: dict | None = None) -> dict:
    """Overlay the saved non-screen settings (SAFE_KEYS) onto `cfg`, in place.

    Separate from apply(), and called before it, because the screen list is
    *derived* from two of these: `display.splits` says how many screens there
    are, and `browser.mode` decides whether a split is allowed at all. They have
    to be in place before there is a list to overlay screen rows onto.
    """
    data = load() if data is None else data
    for section, key in SAFE_KEYS:
        src = data.get(section) if section else data
        if not isinstance(src, dict):
            continue
        # `in`, not truthiness: a saved value of 0 or "" is a real answer here,
        # while the screen fields want the opposite rule -- which is the other
        # reason these are two functions.
        if key in src:
            (cfg[section] if section else cfg)[key] = src[key]
    return cfg


def _an_object(row) -> bool:
    if isinstance(row, dict):
        return True
    log.error("settings.json: a screen entry is %s, not an object — ignoring it",
              type(row).__name__)
    return False


def _overlay(screen: dict, over: dict) -> None:
    for f in SCREEN_FIELDS:
        # Truthy, not `is not None`: blanking a field in the UI must fall back
        # to what was detected rather than store an empty name and leave a
        # screen unaddressable.
        if over.get(f):
            screen[f] = over[f]
