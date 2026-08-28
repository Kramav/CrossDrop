"""Monitor power, as opposed to browser.py's page control.

The Pi has no keyboard or mouse, so anything that blanks the screen and wakes
only on *input* is a trap: the only cure left is unplugging the box. So the agent
takes DPMS off the session's hands — automatic timeouts zeroed, power driven
explicitly — and every /v1 call that touches a screen wakes the display first.
The thing that turns the monitors back on is the same thing you already use to
put something on them.

X11 only, because that is what the Pi runs (Xorg + openbox under lightdm).
"""

import contextlib
import os
import re
import shutil
import subprocess
import threading
import time

# Overridable per deployment via a [display] block, but the defaults are the
# point: nobody should have to edit a config file to stop a display sleeping.
# 0 disables that timer.
DEFAULTS = {"idle_off_minutes": 10, "content_off_minutes": 120}

TICK = 60.0                                     # seconds between idle checks

# xrandr --listmonitors:  " 0: +*HDMI-1 1366/609x768/347+0+0  HDMI-1"
#                                ^name  ^w    ^h        ^x ^y
_MONITOR = re.compile(r"^\s*\d+:\s+\+?\*?(\S+)\s+(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)")

_ok: bool | None = None                         # xset usable? probed once
_on = True                                      # last power state we set
_last: dict[str, float] = {}                    # screen name -> monotonic of last activity
_content: dict[str, bool] = {}                  # screen name -> showing something but home


def _run(argv: list[str]) -> str | None:
    """Run an X tool, or None if it isn't usable. Never raises: a missing tool or
    a session that moved out from under us must cost the display power feature,
    not the kiosk. update.sh ships code without re-running setup.sh, so this
    module has to assume the box may not match what it was installed on."""
    global _ok
    if _ok is None:
        _ok = bool(shutil.which("xset") and os.environ.get("DISPLAY"))
        if not _ok:
            print("display: no xset or no DISPLAY, display power disabled", flush=True)
    if not _ok:
        return None
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=5,
                              check=True).stdout
    except (OSError, subprocess.SubprocessError) as e:
        print(f"display: {' '.join(argv)} failed: {e}", flush=True)
        return None


def claim() -> None:
    """Take ownership of display power for this session.

    DPMS stays *enabled* — `force off`/`force on` need it — but with every
    automatic timeout zeroed, so nothing sleeps unless this agent says so. The
    `+dpms` matters: `raspi-config nonint do_blanking` disables DPMS outright on
    X11, which would leave `force off` a no-op. Correct in either order.
    """
    global _on
    for args in (["+dpms"], ["dpms", "0", "0", "0"], ["s", "off"], ["s", "noblank"]):
        _run(["xset", *args])
    # Sync our idea of the state to reality: the display may well be dark right
    # now (that is the bug this feature exists for), and without this the first
    # touch() would see no transition and leave it dark.
    _on = False
    power(True)


def power(on: bool) -> None:
    """Turn the display on or off. Whole display, both monitors.

    ponytail: no per-monitor control, because X11 has none — `xrandr --prop`
    reports zero DPMS properties on either output. Per-monitor means
    `xrandr --output HDMI-2 --off`, which drops the CRTC, reflows the layout and
    costs a browser.place() plus the scroll position on wake. Under wlroots this
    would be `wlopm --off <output>` and would be per-monitor for free — see
    PLAN.md §7 future considerations.
    """
    global _on
    if on == _on:
        return
    if _run(["xset", "dpms", "force", "on" if on else "off"]) is not None:
        _on = on
        print(f"display: {'on' if on else 'off'}", flush=True)


def awake() -> bool:
    return _on


def detect() -> list[dict]:
    """The monitors X actually has, left to right.

    `xrandr --listmonitors` gives name, size and position on one line, which is
    exactly the three things a [[screen]] block needs — so the agent can read
    the layout instead of being told it.
    """
    out = _run(["xrandr", "--listmonitors"])
    found = [{"output": m[1], "position": f"{m[4]},{m[5]}", "size": f"{m[2]}x{m[3]}"}
             for m in (_MONITOR.match(ln) for ln in (out or "").splitlines()) if m]
    return sorted(found, key=lambda s: int(s["position"].split(",")[0]))


def touch(screen: dict, url: str | None = None) -> None:
    """Record activity on `screen` and wake the display.

    `url` tells us whether the screen is showing something or sitting on its own
    home page — recorded here, at navigate time, so the idle check never has to
    ask the browser anything.
    """
    _last[screen["name"]] = time.monotonic()
    if url is not None:
        _content[screen["name"]] = url != screen["home_url"]
    power(True)


def watch(cfg: dict) -> threading.Event:
    """Sleep the display once every screen has gone quiet. Returns its stop event.

    Only ever turns the display *off*: waking is touch()'s job, driven by a real
    request, so there is no path where this thread lights the room at 3am.
    """
    stop = threading.Event()
    now = time.monotonic()
    for s in cfg["screens"]:                    # unseen == idle since we started
        _last.setdefault(s["name"], now)

    def run() -> None:
        while not stop.wait(TICK):
            with contextlib.suppress(Exception):
                if _all_idle(cfg):
                    power(False)

    threading.Thread(target=run, daemon=True).start()
    return stop


def _all_idle(cfg: dict) -> bool:
    """All monitors sleep together (see power()), so one busy screen keeps the
    display up — which is also the behaviour you want when a second monitor is
    holding a reference open beside the one you're reading."""
    d = cfg.get("display") or DEFAULTS
    now = time.monotonic()
    for s in cfg["screens"]:
        minutes = d["content_off_minutes"] if _content.get(s["name"]) \
            else d["idle_off_minutes"]
        if not minutes or now - _last.get(s["name"], now) < minutes * 60:
            return False
    return True
