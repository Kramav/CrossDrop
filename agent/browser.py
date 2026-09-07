"""Launch a kiosk browser and drive its tab.

Two protocols, because Firefox dropped CDP in 129 (we're on 140):
  - chromium/edge -> CDP        (Page.navigate)
  - firefox       -> WebDriver BiDi (browsingContext.navigate)
Both are JSON-RPC over one websocket, so `_rpc` serves both.
"""

import contextlib
import itertools
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import websocket

from . import extensions

# A child of app.py's "room" logger: same level, same journal. The print()s
# elsewhere here are startup notices; this is what you go looking for after a
# screen has behaved oddly.
log = logging.getLogger("room.browser")

CANDIDATES = {
    "firefox": [
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
        "/usr/bin/firefox",
    ],
    "edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "chromium": [
        # Trixie Pi OS ships Debian's chromium -> /usr/bin/chromium. Bookworm and
        # earlier shipped Raspberry Pi's own build -> /usr/bin/chromium-browser.
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ],
}


# What each backend can do, reported by /v1/status as `supports`, so a program
# checks once instead of discovering the API by collecting 501s. The dev box
# ships firefox and the Pi chromium, so these really are different APIs.
#
# The table is also the *enforcement*: _require() reads it, so a name missing
# here 501s without anyone writing the check. It replaced six hand-copied
# `if kind == "firefox": raise` blocks -- a sync nobody could see going wrong.
#
# Routes that never touch the browser (display, upload) are not listed.
_CDP_ONLY = ("scroll", "autoscroll", "media", "screens", "window", "extensions",
             "screenshot", "inspect", "input")
SUPPORTS = {
    "chromium": ("navigate", *_CDP_ONLY),
    "edge": ("navigate", *_CDP_ONLY),
    # Firefox: BiDi gives us navigation, and one session for the whole browser
    # (see _bidi_conns), so there is no second window to address.
    "firefox": ("navigate",),
}


def _require(cfg: dict, feature: str) -> None:
    """501 unless this backend does `feature`. Reads SUPPORTS, so the table and
    the refusal can never drift apart.

    Not supports(): that one also hides `input` when [interact] is off, which is
    the route's own check and a different answer (app.py names the config key).
    """
    kind = cfg["browser"]["kind"]
    # Unknown kinds take the CDP path everywhere else in this file.
    if feature not in SUPPORTS.get(kind, SUPPORTS["chromium"]):
        raise NotImplementedError(
            f"{feature} needs CDP; use kind = \"chromium\" or \"edge\"")


def interactive(cfg: dict) -> bool:
    """Is `POST /v1/input` switched on? Off unless config.toml says otherwise --
    it is the only route that acts *as* whoever the kiosk is logged in as. File
    only, in the config the agent cannot write (root:<user> 640): a switch the
    API can turn on for itself is not a switch."""
    return bool((cfg.get("interact") or {}).get("enabled"))


def supports(cfg: dict) -> list[str]:
    # Unknown kinds take the CDP path everywhere else in this file, so they get
    # the CDP answer here too rather than a conservative lie.
    names = list(SUPPORTS.get(cfg["browser"]["kind"], SUPPORTS["chromium"]))
    if not interactive(cfg):
        # Absent, not merely refused: a client reads `supports` to decide what
        # to offer, and the web UI hides a control it finds missing. A disabled
        # agent should look exactly like one that cannot do it.
        names = [n for n in names if n != "input"]
    return names


def _exe(kind: str, path: str = "") -> str:
    if path:
        return path
    for c in CANDIDATES.get(kind, []):
        if Path(c).exists():
            return c
    found = shutil.which(kind) or shutil.which(f"{kind}-browser")
    if not found:
        raise RuntimeError(f"no {kind} binary found; set browser.path in config")
    return found


def screens(cfg: dict) -> list[dict]:
    # Configs built before multi-monitor have no "screens" key at all.
    return cfg.get("screens") or [{"name": "main", "position": "",
                                   "home_url": cfg.get("home_url", "about:blank")}]


def launch(cfg: dict) -> subprocess.Popen:
    """Start the kiosk browser and block until its debug port answers.

    One browser, one profile, one debug port, one window per screen. Two
    instances would double the RAM on a tmpfs profile and split your logins
    across two cookie stores (PLAN.md §6 SSO note).
    """
    b = cfg["browser"]
    kind, port = b["kind"], b["debug_port"]
    _loaded.clear()             # firefox takes none; chromium fills this below
    profile = Path(b["profile_dir"])
    profile.mkdir(parents=True, exist_ok=True)
    scr = screens(cfg)
    home = scr[0]["home_url"]

    if kind == "firefox":
        argv = [_exe(kind, b["path"]), "--remote-debugging-port", str(port),
                "--profile", str(profile), "--no-remote", "--kiosk", home]
    else:
        argv = [_exe(kind, b["path"]), f"--remote-debugging-port={port}",
                # No --remote-allow-origins=*. Chrome >= 111 blocks CDP
                # websockets carrying an Origin specifically to keep page
                # content off the debug port, and the page here is arbitrary by
                # design. _rpc sends no Origin at all, so the flag bought
                # nothing and disabled that defense for every rendered page.
                f"--user-data-dir={profile}", "--kiosk",
                # Never the system keyring: under desktop autologin the login
                # keyring is locked (nobody typed a password), so libsecret puts
                # a modal unlock dialog over the kiosk, forever. "basic" is
                # Chromium's own store; ignored on Windows.
                "--password-store=basic",
                # Same class of problem: after a power cut Chromium offers to
                # restore the last session in a bubble nobody can dismiss.
                "--disable-session-crashed-bubble",
                # The profile is on tmpfs, so this cache is RAM. Uncapped,
                # Chromium sizes it from free space and fills /run/user/<uid>.
                f"--disk-cache-size={b['disk_cache_mb'] * 1024 * 1024}",
                # Chromium blocks autoplay with sound until someone clicks, and
                # nobody can click this box. /v1/media sends userGesture anyway,
                # so this only covers arriving on a page that autoplays.
                "--autoplay-policy=no-user-gesture-required",
                "--no-first-run", "--no-default-browser-check"]
        # Unpacked, because the kiosk has no install UI and Debian's Chromium
        # ignores ExtensionInstallForcelist (deploy/pi/README.md §10). A
        # directory, so installing one is a file operation, not a config edit.
        _loaded[:] = extensions.scan(b.get("extensions_dir", ""))
        if _loaded:
            print(f"browser: loading {len(_loaded)} extension(s): "
                  f"{', '.join(extensions.display_name(e) for e in _loaded)}",
                  flush=True)
            argv += [f"--load-extension={','.join(_loaded)}",
                     # Chromium 137 disabled --load-extension outside dev builds;
                     # turning that feature off is the supported way back in. If a
                     # future build renames the feature the flag becomes inert and
                     # the extension silently stops loading -- check the argv
                     # against `chromium --help` before assuming the path is wrong.
                     "--disable-features=DisableLoadExtensionCommandLineSwitch"]
        # Wayland gives the compositor final say on position and Chromium
        # ignores --window-position there; under XWayland the move is an X11
        # configure request, which labwc honours. deploy/pi/README.md.
        if len(scr) > 1:
            argv += ["--ozone-platform=x11"]
        if scr[0]["position"]:
            argv += [f"--window-position={scr[0]['position']}"]
        argv += [home]

    # own process group on POSIX so stop() can take the whole tree down
    proc = subprocess.Popen(argv, start_new_session=os.name != "nt")
    wait_ready(kind, port)

    _targets.clear()
    if len(scr) > 1 or scr[0]["position"]:
        # Window 1 already exists (--kiosk put it wherever the compositor
        # liked), so it is *moved*. Without this its `position` does nothing and
        # choosing its monitor means reordering the config until it guesses.
        place(cfg, scr[0])
    for s in scr[1:]:
        open_window(cfg, s)
    return proc


def _pair(value: str, sep: str, field: str) -> tuple[int, int]:
    """Parse "1366,0" / "2560x1440". A typo breaks the kiosk at boot on a box
    with no keyboard, so the error names the offending value."""
    try:
        a, b = value.split(sep)
        return int(a), int(b)
    except ValueError:
        raise RuntimeError(
            f"screen {field} must look like {sep.join(('1920', '1080'))}, got {value!r}")


def _place(call, target_id: str, position: str, size: str = "") -> None:
    """Move a window onto the monitor containing `position`, then fullscreen it."""
    if not position:
        return
    x, y = _pair(position, ",", "position")
    # Sized to the monitor when we know it: the window is briefly visible
    # between the move and the fullscreen. Cosmetic -- fullscreen overrides.
    w, h = _pair(size, "x", "size") if size else (800, 600)
    win = call("Browser.getWindowForTarget", {"targetId": target_id})["windowId"]
    # Move first, fullscreen second: Chromium refuses to move a window that is
    # already fullscreen, and "normal" is what un-fullscreens a --kiosk one.
    call("Browser.setWindowBounds", {"windowId": win, "bounds": {
        "left": x, "top": y, "width": w, "height": h, "windowState": "normal"}})
    # ponytail: let the move land before asking for fullscreen. CDP returns as
    # soon as Chromium has *sent* the request; a compositor that applies it
    # asynchronously would otherwise fullscreen against the window's old output
    # and put it straight back where it started. 300ms, not a handshake, because
    # there is no event to wait on -- raise it if placement is ever flaky.
    time.sleep(PLACE_SETTLE)
    call("Browser.setWindowBounds",
         {"windowId": win, "bounds": {"windowState": "fullscreen"}})


def place(cfg: dict, screen: dict) -> str:
    """Move the window already belonging to `screen` onto its monitor."""
    _require(cfg, "screens")
    port = cfg["browser"]["debug_port"]
    page = _cdp_page(cfg, screen["name"])
    with _rpc(_get(port, "/json/version")["webSocketDebuggerUrl"]) as call:
        _place(call, page["id"], screen["position"], screen.get("size", ""))
    return page["id"]


WINDOW_STATES = ("normal", "minimized", "fullscreen")


def window(cfg: dict, screen: dict, state: str) -> str:
    """Put one screen's kiosk window aside, or back.

    The escape hatch from --kiosk: minimized, the Pi's own desktop is reachable
    without stopping the agent, which is otherwise the only way in.

    "fullscreen" goes via "normal" for the same reason _place() does: Chromium
    will not transition straight out of minimized, and a --kiosk window has to
    be un-fullscreened before its bounds can change.

    ponytail: fire-and-forget, so a navigate to a minimized window renders
    offscreen until someone asks for fullscreen again. Read windowState back out
    of Browser.getWindowForTarget and carry it in ScreenOut if that has to be
    visible -- it costs a CDP roundtrip on every status poll.
    """
    _require(cfg, "window")
    if state not in WINDOW_STATES:
        raise ValueError(f"state must be one of {', '.join(WINDOW_STATES)}")
    page = _cdp_page(cfg, screen["name"])
    # A screen with a position has a placement to go back to, and place()
    # already does normal -> move -> fullscreen. Restoring without it would
    # fullscreen against whichever monitor the window happens to be on.
    if state == "fullscreen" and screen.get("position"):
        place(cfg, screen)
        return page["url"] or "about:blank"
    port = cfg["browser"]["debug_port"]
    with _rpc(_get(port, "/json/version")["webSocketDebuggerUrl"]) as call:
        win = call("Browser.getWindowForTarget", {"targetId": page["id"]})["windowId"]
        if state == "fullscreen":
            call("Browser.setWindowBounds",
                 {"windowId": win, "bounds": {"windowState": "normal"}})
            time.sleep(PLACE_SETTLE)
        call("Browser.setWindowBounds", {"windowId": win, "bounds": {"windowState": state}})
    return page["url"] or "about:blank"


def open_window(cfg: dict, screen: dict) -> str:
    """Open a fullscreen window for `screen` and return its CDP target id."""
    _require(cfg, "screens")
    port = cfg["browser"]["debug_port"]
    with _rpc(_get(port, "/json/version")["webSocketDebuggerUrl"]) as call:
        tid = call("Target.createTarget",
                   {"url": screen["home_url"], "newWindow": True})["targetId"]
        _place(call, tid, screen["position"], screen.get("size", ""))
    _targets[screen["name"]] = tid
    return tid


def _require_addressable(cfg: dict, screen: str | None) -> None:
    """Refuse a screen Firefox cannot actually reach.

    One BiDi session per browser means one addressable window, so `screen` has
    nowhere to go here. Driving the first monitor instead is the worst answer: a
    caller asking for `all` gets two successes and one changed monitor, and
    nothing says so. Fail, as a 501, the same way scroll and media do.
    """
    first = screens(cfg)[0]["name"]
    if screen and screen != first:
        raise NotImplementedError(
            f"screen {screen!r} needs CDP; firefox drives only {first!r}")


def stop(cfg: dict, proc: subprocess.Popen) -> None:
    """Shut the browser down *and its children*. Both browsers fork a process
    tree, and terminating the launcher alone leaves a fullscreen kiosk on screen
    and the debug port held — on a box with no keyboard, forever.

    Over the debug protocol first, because the pid is not a reliable handle: on
    Windows the msedge/chrome launcher exits the moment it hands off, so
    `taskkill /T` walks a tree no longer rooted at that pid.
    """
    b = cfg["browser"]
    kind, port = b["kind"], b["debug_port"]

    with contextlib.suppress(Exception):
        if kind == "firefox":
            _bidi(port, "browser.close")
        else:
            with _rpc(_get(port, "/json/version")["webSocketDebuggerUrl"]) as call:
                call("Browser.close")
    close()
    if _wait_gone(kind, port):
        return

    # Wedged, or a build that ignored the request: fall back to the pid tree.
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(10)


def _wait_gone(kind: str, port: int, timeout: float = 10.0) -> bool:
    """True once the debug port stops answering. The port, not the pid, is what
    the next agent start collides with."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            wait_ready(kind, port, timeout=0.5)
        except RuntimeError:
            return True
        time.sleep(0.3)
    return False


def wait_ready(kind: str, port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if kind == "firefox":
                # BiDi has no HTTP discovery — the socket opening is the signal.
                websocket.create_connection(_bidi_url(port), timeout=5,
                                            suppress_origin=True).close()
            else:
                _get(port, "/json/version", latch=False)
            return
        except (OSError, websocket.WebSocketException):
            if time.monotonic() > deadline:
                raise RuntimeError(f"debug port {port} never came up")
            time.sleep(0.3)


def navigate(cfg: dict, url: str, screen: str | None = None) -> str:
    """Point one screen's window at `url`. Returns the url we sent it to."""
    b = cfg["browser"]
    if b["kind"] == "firefox":
        _require_addressable(cfg, screen)
        port = b["debug_port"]
        # "interactive", not "none", so /v1/navigate means the page committed --
        # matching CDP's Page.navigate. Slower than the 15s timeout is a 503.
        _bidi(port, "browsingContext.navigate",
              {"context": _top_context(port)["context"], "url": url,
               "wait": "interactive"})
    else:
        page = _cdp_page(cfg, screen)
        with _rpc(page["webSocketDebuggerUrl"]) as call:
            call("Page.navigate", {"url": url})
    return url


def current_url(cfg: dict, screen: str | None = None) -> str:
    b = cfg["browser"]
    if b["kind"] == "firefox":
        _require_addressable(cfg, screen)
        return _top_context(b["debug_port"])["url"]
    # CDP reports a blank tab as "", BiDi as "about:blank". Normalise, or the two
    # backends disagree and /v1/reload tries to navigate to the empty string.
    return _cdp_page(cfg, screen)["url"] or "about:blank"


# Far enough to hit the end of anything: scroll offsets clamp, so one oversized
# wheel event lands exactly at the top or bottom. Key events (Home/End) look
# tidier but do not reliably reach the PDF viewer's embedded frame.
_JUMP = {"top": -10_000_000, "bottom": 10_000_000}


def scroll(cfg: dict, screen: str | None = None, dy: int = 0,
           to: str | None = None) -> None:
    """Scroll one screen. `to` jumps to top/bottom, otherwise `dy` pixels.

    A real wheel event, not `window.scrollBy`: Chromium's PDF viewer is a plugin
    that ignores scripted window scrolling, and a PDF is half of what this
    display is for.
    """
    _require(cfg, "scroll")
    if to is not None and to not in _JUMP:
        raise ValueError(f"to must be one of {sorted(_JUMP)}")
    page = _cdp_page(cfg, screen)
    with _rpc(page["webSocketDebuggerUrl"]) as call:
        # Aim at the middle of the viewport. A fixed point near the top-left
        # lands in the PDF viewer's thumbnail sidebar, and scrolls *that*.
        x, y = _viewport_centre(call)
        call("Input.dispatchMouseEvent",
             {"type": "mouseWheel", "x": x, "y": y,
              "deltaX": 0, "deltaY": _JUMP[to] if to else dy})


AUTOSCROLL_TICK = 0.1

# Seconds of scrolling per synthesised gesture: longer means fewer round-trips,
# shorter means `stop` bites sooner, since a gesture runs to completion first.
# Tunable, because how smooth this looks is a property of the panel and GPU.
GESTURE_SECS = float(os.getenv("ROOM_GESTURE_SECS", "0.5"))


def autoscroll(cfg: dict, screen: str | None, speed: int,
               stop: threading.Event) -> None:
    """Scroll one screen `speed` pixels a tick until `stop` is set.

    Smoothness is the point. Ten wheel events a second is ten visible steps;
    Input.synthesizeScrollGesture hands the whole movement to Chromium, which
    interpolates it at the compositor's frame rate — smoother *and* two calls a
    second instead of ten, on a Pi already busy rendering the page.

    `speed` is still pixels per AUTOSCROLL_TICK, so the CLI flag and the UI
    slider keep their values; the gesture API is told pixels per second.

    One connection for the whole run, not one per tick (PLAN.md §11 finding 5),
    and the viewport centre read once — a fullscreen kiosk does not resize. A
    window closing under us kills the socket and ends the run, which is right:
    the only other thing that changes a target is a navigation, and that already
    stops autoscroll deliberately.
    """
    _require(cfg, "autoscroll")
    page = _cdp_page(cfg, screen)
    with _rpc(page["webSocketDebuggerUrl"]) as call:
        x, y = _viewport_centre(call)
        if not speed:
            stop.wait()         # a 0 px/s autoscroll is a no-op, not a spin
            return
        px_s = abs(speed) / AUTOSCROLL_TICK
        smooth = True
        while not stop.is_set():
            if smooth:
                started = time.monotonic()
                try:
                    call("Input.synthesizeScrollGesture", {
                        "x": x, "y": y,
                        # yDistance is positive to scroll *up*, the opposite of
                        # a wheel event's deltaY. Getting this backwards silently
                        # scrolls the wrong way, so it is negated here once.
                        "yDistance": -(speed / AUTOSCROLL_TICK) * GESTURE_SECS,
                        "speed": px_s,
                        # No momentum: a wall display should stop where it stops.
                        "preventFling": True})
                except RuntimeError as e:
                    # Experimental API; an old build may not have it. Drop to
                    # wheel ticks rather than end a scroll someone asked for.
                    print(f"autoscroll: no smooth gesture ({e}); "
                          f"falling back to wheel ticks", flush=True)
                    smooth = False
                    continue
                # The call returns when the gesture finishes, so normally
                # nothing is left to wait for. This is what stops a target that
                # answered instantly spinning the loop at 100% CPU.
                left = GESTURE_SECS - (time.monotonic() - started)
                if left > 0 and stop.wait(left):
                    break
                continue
            if stop.wait(AUTOSCROLL_TICK):
                break
            call("Input.dispatchMouseEvent",
                 {"type": "mouseWheel", "x": x, "y": y,
                  "deltaX": 0, "deltaY": speed})


# --- input ------------------------------------------------------------------
# Click and type, for the one thing this display cannot do otherwise: no
# keyboard, so an expired SSO login or a consent wall is a page nobody can get
# past (PLAN.md §6 "SSO expiry").
#
# Everything goes into one CDP *target* -- the connection navigate uses. It
# reaches that window's renderer and nothing else: no alt-tab, no window
# manager, no other application. The boundary is a property of the transport
# rather than a rule anyone has to remember.

INPUT_ACTIONS = ("click", "double", "right", "move", "drag", "type", "key", "wait")

# Modifier bits, as CDP wants them.
_MODIFIERS = {"alt": 1, "ctrl": 2, "control": 2, "meta": 4, "cmd": 4, "shift": 8}

# Everything a login form or a PDF viewer needs, as
# name -> (windowsVirtualKeyCode, key, text). A single printable character is
# handled separately; anything else is refused rather than guessed at.
_KEYS = {
    "Enter": (13, "Enter", "\r"), "Tab": (9, "Tab", "\t"),
    "Escape": (27, "Escape", ""), "Backspace": (8, "Backspace", ""),
    "Delete": (46, "Delete", ""), "Space": (32, " ", " "),
    "ArrowUp": (38, "ArrowUp", ""), "ArrowDown": (40, "ArrowDown", ""),
    "ArrowLeft": (37, "ArrowLeft", ""), "ArrowRight": (39, "ArrowRight", ""),
    "Home": (36, "Home", ""), "End": (35, "End", ""),
    "PageUp": (33, "PageUp", ""), "PageDown": (34, "PageDown", ""),
}

_BUTTONS = {"click": ("left", 1), "double": ("left", 2), "right": ("right", 1)}

# Where to click an element. scrollIntoView first: one below the fold has
# coordinates outside the viewport, and a click there hits nothing while
# reporting success.
_FIND_JS = """(() => {
  const el = document.querySelector(%(selector)s);
  if (!el) return null;
  el.scrollIntoView({block: 'center', inline: 'center'});
  const r = el.getBoundingClientRect();
  if (r.width < 1 || r.height < 1) return null;
  return {x: Math.round(r.left + r.width / 2),
          y: Math.round(r.top + r.height / 2)};
})()"""


def _mouse(call, kind: str, x: int, y: int, clicks: int = 1,
           button: str = "left") -> None:
    call("Input.dispatchMouseEvent",
         {"type": kind, "x": x, "y": y, "button": button, "clickCount": clicks})


def _find(call, selector: str) -> tuple[int, int]:
    at = _evaluate(call, _FIND_JS % {"selector": json.dumps(selector)}, "find")
    if not at:
        raise ValueError(f"no visible element matches {selector!r}")
    return int(at["x"]), int(at["y"])


def _key_spec(key: str, modifiers: list) -> dict:
    """Resolve a key and its modifiers into CDP's fields, or raise. Pure, so the
    whole action list can be checked before any of it is dispatched."""
    bits = 0
    for m in modifiers:
        if str(m).lower() not in _MODIFIERS:
            raise ValueError(f"unknown modifier {m!r}; "
                             f"have: {', '.join(sorted(set(_MODIFIERS)))}")
        bits |= _MODIFIERS[str(m).lower()]
    if key in _KEYS:
        code, name, text = _KEYS[key]
    elif len(key) == 1:
        # A printable character. keyCode is the uppercase form, which is what a
        # physical keyboard reports and what shortcut handlers match on.
        code, name, text = ord(key.upper()), key, key
    else:
        raise ValueError(f"unknown key {key!r}; a single character, or one of: "
                         f"{', '.join(sorted(_KEYS))}")
    # A modified key carries no text: ctrl+a is a shortcut, not the letter "a",
    # and sending text with it selects all *and* types an "a". Shift is the
    # exception -- shift+A is genuinely the character.
    return {"key": name, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code,
            "modifiers": bits, "text": "" if bits & ~8 else text}


def _check(a: dict) -> None:
    """Everything about one action that can be wrong without asking the browser.

    Run over the whole list before any of it executes: there is no undo, and a
    typo in action 3 must not be found after 1 and 2 have clicked something and
    typed into it. Same rule as /v1/extensions -- a typo is total.
    """
    do = a.get("do")
    if do not in INPUT_ACTIONS:
        raise ValueError(f"unknown action {do!r}; have: {', '.join(INPUT_ACTIONS)}")
    if do in _BUTTONS or do == "move":
        if not a.get("selector"):
            _point((a.get("x"), a.get("y")), "x/y")
    elif do == "drag":
        _point(a.get("from"), "from")
        _point(a.get("to"), "to")
    elif do == "type":
        if not isinstance(a.get("text"), str) or not a["text"]:
            raise ValueError("type needs a non-empty text")
    elif do == "key":
        _key_spec(str(a.get("key") or ""), list(a.get("modifiers") or []))
    elif do == "wait":
        ms = a.get("ms", 0)
        if not isinstance(ms, int) or isinstance(ms, bool) or not 0 <= ms <= 10_000:
            raise ValueError(f"wait ms must be an integer 0-10000, got {ms!r}")


def _act(call, a: dict) -> dict:
    """Perform one already-checked action. Returns what is worth reporting."""
    do = a["do"]

    if do in _BUTTONS:
        button, clicks = _BUTTONS[do]
        x, y = _at(call, a)
        _mouse(call, "mousePressed", x, y, clicks, button)
        _mouse(call, "mouseReleased", x, y, clicks, button)
        return {"x": x, "y": y}

    if do == "move":
        x, y = _at(call, a)
        _mouse(call, "mouseMoved", x, y)
        return {"x": x, "y": y}

    if do == "drag":
        x0, y0 = _point(a.get("from"), "from")
        x1, y1 = _point(a.get("to"), "to")
        _mouse(call, "mousePressed", x0, y0)
        # Intermediate moves: HTML5 drag-and-drop and canvas apps want to see
        # the pointer travel, and a single jump reads as a stray click.
        for i in range(1, 6):
            _mouse(call, "mouseMoved", x0 + (x1 - x0) * i // 5,
                   y0 + (y1 - y0) * i // 5)
        _mouse(call, "mouseReleased", x1, y1)
        return {"x": x1, "y": y1}

    if do == "type":
        # insertText, not a key event per character: one round trip instead of
        # two per letter, and it still fires the beforeinput/input a
        # framework-controlled field listens for.
        call("Input.insertText", {"text": a["text"]})
        return {}

    if do == "key":
        spec = _key_spec(str(a.get("key") or ""), list(a.get("modifiers") or []))
        # Both events, always: a page that sees a key pressed and never released
        # keeps thinking a modifier is held down.
        call("Input.dispatchKeyEvent",
             {"type": "keyDown" if spec["text"] else "rawKeyDown", **spec})
        call("Input.dispatchKeyEvent", {**spec, "type": "keyUp", "text": ""})
        return {}

    time.sleep(int(a.get("ms", 0)) / 1000)
    return {}


def _at(call, a: dict) -> tuple[int, int]:
    """Where an action points: a selector if it has one, else coordinates."""
    if a.get("selector"):
        return _find(call, str(a["selector"]))
    return _point((a.get("x"), a.get("y")), "x/y")


def _point(pair, what: str) -> tuple[int, int]:
    try:
        x, y = pair
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be two numbers, got {pair!r}")
    if x < 0 or y < 0:
        raise ValueError(f"{what} must not be negative, got ({x}, {y})")
    return x, y


def input(cfg: dict, screen: str | None, actions: list[dict],
          deadline: float | None = None) -> list[dict]:
    """Run `actions` against one screen, in order, over one connection.

    A list rather than a route per verb: a login is five actions, and as five
    requests that is five websockets and five chances to interleave. One
    request is one connection, one ordering, one audit record.

    **Stops at the first failure**, because if the click that focuses the
    password box missed, the next action types the password into whatever does
    have focus. The result list says which action stopped it.

    `deadline` is a time.monotonic() value, checked between actions so a long
    list cannot hold a threadpool thread indefinitely.
    """
    # BiDi has input.performActions, so on firefox this is unwritten rather than
    # impossible -- but the Pi runs chromium. Same 501 as scroll and media.
    _require(cfg, "input")
    if not actions:
        raise ValueError("no actions")
    for i, a in enumerate(actions):
        try:
            _check(a)
        except ValueError as e:
            raise ValueError(f"action {i}: {e}")    # nothing has run yet

    page = _cdp_page(cfg, screen)
    results: list[dict] = []
    with _rpc(page["webSocketDebuggerUrl"]) as call:
        for a in actions:
            if deadline is not None and time.monotonic() > deadline:
                results.append({"do": a["do"], "ok": False, "took_ms": 0,
                                "error": "deadline exceeded before this action"})
                break
            started = time.monotonic()
            try:
                extra = _act(call, a)
            except Exception as e:      # element gone, socket dropped, protocol
                results.append({"do": a["do"], "ok": False,
                                "took_ms": int((time.monotonic() - started) * 1000),
                                "error": str(e)})
                break                   # never type into whatever has focus now
            results.append({"do": a["do"], "ok": True, **extra,
                            "took_ms": int((time.monotonic() - started) * 1000)})
    return results


MEDIA_ACTIONS = ("state", "play", "pause", "toggle", "mute", "unmute", "seek", "volume")

# Driven through the element itself: there is no CDP "media" domain, and every
# player worth showing on a wall is an HTML5 <video>/<audio> underneath.
#
# ponytail: top frame, main world, no shadow DOM -- a cross-origin <iframe>
# player (embedded YouTube, not youtube.com) reports nothing playing. Reaching
# those needs a per-frame execution context: Runtime.enable and events.
_MEDIA_JS = """(() => {
  const els = [...document.querySelectorAll('video, audio')];
  // Biggest first: a page with an autoplaying banner clip beside the real video
  // must not hand back the banner. Audio elements have no box, hence the || 1.
  const m = els.sort((a, b) => (b.clientWidth * b.clientHeight || 1)
                             - (a.clientWidth * a.clientHeight || 1))[0];
  if (!m) return null;
  const a = %(action)s, v = %(value)s;
  if (a === 'play') m.play();
  else if (a === 'pause') m.pause();
  else if (a === 'toggle') m.paused ? m.play() : m.pause();
  else if (a === 'mute' || a === 'unmute') m.muted = a === 'mute';
  else if (a === 'seek') m.currentTime = Math.max(0, (m.currentTime || 0) + v);
  // Unmute on a volume change: nudging the slider on a muted video and hearing
  // nothing is indistinguishable from the whole feature being broken.
  else if (a === 'volume') { m.volume = Math.min(1, Math.max(0, v / 100)); m.muted = false; }
  return {playing: !m.paused, muted: !!m.muted, volume: Math.round(m.volume * 100),
          position: Math.round(m.currentTime || 0),
          duration: Math.round(isFinite(m.duration) ? m.duration : 0)};
})()"""


def media(cfg: dict, screen: str | None = None, action: str = "state",
          value: float = 0) -> dict | None:
    """Drive the <video>/<audio> on one screen. None if the page has none.

    `state` only reports. `seek` takes seconds (negative rewinds), `volume` 0-100.
    """
    _require(cfg, "media")
    if action not in MEDIA_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(MEDIA_ACTIONS)}")
    page = _cdp_page(cfg, screen)
    with _rpc(page["webSocketDebuggerUrl"]) as call:
        return _evaluate(call, _MEDIA_JS % {"action": json.dumps(action),
                                            "value": json.dumps(float(value))},
                         f"media {action}")


def _viewport(call) -> tuple[int, int]:
    """The window's CSS-pixel viewport. The 800x600 fallback is a guess that
    keeps a scroll off the PDF sidebar and gives a screenshot a clip rather than
    an exception."""
    with contextlib.suppress(Exception):        # older builds, odd targets
        v = call("Page.getLayoutMetrics").get("cssLayoutViewport") or {}
        w, h = v.get("clientWidth"), v.get("clientHeight")
        if w and h:
            return w, h
    return 800, 600


def _viewport_centre(call) -> tuple[int, int]:
    w, h = _viewport(call)
    return w // 2, h // 2                       # better than a sidebar hit


SCREENSHOT_FORMATS = ("png", "jpeg", "webp")


def screenshot(cfg: dict, screen: str | None = None, region: dict | None = None,
               format: str = "png", quality: int = 80) -> dict:
    """Capture what one screen's window is showing. Returns base64 image + size.

    The read-back a url cannot be: /v1/navigate reports what we *sent*, so a
    redirect, a login wall or an "Aw, Snap!" looks like success everywhere else.

    The clip is always sent, always at `scale: 1` — that is the whole coordinate
    contract. Without it Chromium captures at the device pixel ratio, so a
    1920-wide viewport on a HiDPI panel comes back 3840 wide and every mapping
    back onto the page is off by two. Pinned, **image pixels are CSS pixels**,
    the same space Input.dispatchMouseEvent takes.

    `region` is clamped rather than rejected, so the returned width/height are
    what you got, not necessarily what you asked for.

    A page capture, not a screen capture: it renders one browser target's frame
    tree and can no more see the desktop than Page.navigate can drive it.
    """
    # BiDi does have browsingContext.captureScreenshot, so on firefox this is
    # unwritten rather than impossible. Same 501 either way.
    _require(cfg, "screenshot")
    if format not in SCREENSHOT_FORMATS:
        raise ValueError(f"format must be one of {', '.join(SCREENSHOT_FORMATS)}")
    if not 1 <= quality <= 100:
        raise ValueError(f"quality must be 1-100, got {quality}")

    page = _cdp_page(cfg, screen)
    with _rpc(page["webSocketDebuggerUrl"]) as call:
        vw, vh = _viewport(call)
        x, y, w, h = _clip(region, vw, vh)
        params = {"format": format, "captureBeyondViewport": False,
                  "clip": {"x": x, "y": y, "width": w, "height": h, "scale": 1}}
        if format != "png":                     # png ignores it and some builds complain
            params["quality"] = quality
        data = call("Page.captureScreenshot", params).get("data") or ""
    return {"image": data, "format": format, "width": w, "height": h,
            # Straight off the target listing, so this costs no extra round
            # trip: what the browser currently calls the page, beside a picture
            # of it. Note "currently" -- Page.navigate returns on commit, so for
            # a moment after one this is Chromium's provisional title, which is
            # the bare host. /v1/inspect reads document.title instead and is the
            # one to ask if you need the real answer the instant you land.
            "url": page.get("url") or "about:blank", "title": page.get("title") or ""}


# Structured page state, for a caller that cannot look at a picture -- eve,
# update.sh, anything deciding whether to retry. No field *values*, ever:
# naming a password box is how a caller knows where to type, returning what is
# in it turns a diagnostic route into a credential leak.
_INSPECT_JS = """(() => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    return r.width >= 1 && r.height >= 1;
  };
  const label = el =>
    (el.labels && el.labels[0] && el.labels[0].textContent) ||
    el.getAttribute('aria-label') || el.placeholder || el.name || '';
  // Only selectors that will still resolve on the next call: an id or a name.
  // An nth-child path would be stable for exactly as long as the page is.
  const selector = el =>
    el.id ? '#' + CSS.escape(el.id)
    : el.name ? el.tagName.toLowerCase() + '[name="' + CSS.escape(el.name) + '"]'
    : '';
  const fields = [...document.querySelectorAll(
      'input, textarea, select, button, [role=button]')]
    .filter(visible)
    .slice(0, 40)                       // a long form is not worth a long reply
    .map(el => ({selector: selector(el), tag: el.tagName.toLowerCase(),
                 type: (el.getAttribute('type') || '').toLowerCase(),
                 label: String(label(el)).trim().slice(0, 80)}))
    .filter(f => f.selector);
  return {
    title: document.title,
    ready_state: document.readyState,
    // Chromium's own error page renders perfectly and answers /v1/status, so
    // "Aw, Snap!" and ERR_CONNECTION_REFUSED look like success everywhere else.
    error_page: !!document.querySelector('#main-frame-error'),
    has_media: !!document.querySelector('video, audio'),
    scroll_y: Math.round(window.scrollY || 0),
    scroll_height: Math.round(document.documentElement.scrollHeight || 0),
    fields: fields,
  };
})()"""


def inspect(cfg: dict, screen: str | None = None) -> dict:
    """What one screen's page says about itself. No image, no field values.

    The machine-readable half of `screenshot`: a program cannot look at a
    picture, and `scroll_y` moving is the only proof an autoscroll is running.
    """
    _require(cfg, "inspect")
    page = _cdp_page(cfg, screen)
    # Off the target listing, so these two survive a page we cannot run script
    # on -- which is the whole point of what follows.
    url = page.get("url") or "about:blank"
    out = {"url": url, "title": page.get("title") or "", "ready_state": "unknown",
           "error_page": False, "has_media": False,
           "scroll_y": 0, "scroll_height": 0, "fields": []}
    try:
        with _rpc(page["webSocketDebuggerUrl"]) as call:
            out.update(_evaluate(call, _INSPECT_JS, "inspect") or {})
    except Exception as e:
        # A page we cannot ask is the one this route most needs to describe.
        # Debian's Chromium refuses Runtime.evaluate on chrome-error:// where
        # Google's allows it, and raising here meant /v1/inspect 503'd on
        # exactly the page it exists to detect: `error_page` could never come
        # back true and update.sh's rollback gate guarded nothing. Found by the
        # smoke suite on the Pi, having passed against desktop Chrome every
        # time. ready_state stays "unknown", which is how a caller tells "still
        # loading" from "would not answer at all".
        log.warning("inspect: no script on %s (%s); reporting what the target "
                    "listing knows", url, e)
    # The second witness, and now reachable: a page that failed to load may have
    # no document to ask, and Chromium parks those on chrome-error://.
    out["error_page"] = bool(out.get("error_page")) or url.startswith("chrome-error")
    return out


def _evaluate(call, expression: str, what: str):
    """Run a page script and return its value, or raise with the page's own
    message -- so a thrown TypeError is a failure, not a silent None."""
    r = call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                  # Chromium refuses play(), and some focus
                                  # handling, on a page nobody has interacted
                                  # with -- and nobody ever interacts with a
                                  # kiosk. This is what a real click gives.
                                  "userGesture": True})
    if r.get("exceptionDetails"):
        raise RuntimeError(
            f"{what} failed: {r['exceptionDetails'].get('text', 'script error')}")
    return r.get("result", {}).get("value")


def _clip(region: dict | None, vw: int, vh: int) -> tuple[int, int, int, int]:
    """A region clamped into the viewport, as (x, y, width, height)."""
    if not region:
        return 0, 0, vw, vh
    x = min(max(int(region.get("x", 0)), 0), max(vw - 1, 0))
    y = min(max(int(region.get("y", 0)), 0), max(vh - 1, 0))
    w = min(int(region.get("width") or vw), vw - x)
    h = min(int(region.get("height") or vh), vh - y)
    if w < 1 or h < 1:
        raise ValueError(
            f"region is empty against a {vw}x{vh} viewport: {region}")
    return x, y, w, h


def close() -> None:
    """Release BiDi sessions: a browser we didn't launch outlives the agent, and
    Firefox won't hand out a second session while the first is open."""
    with _bidi_lock:
        for port, (ws, call) in list(_bidi_conns.items()):
            del _bidi_conns[port]
            for shutdown in (lambda: call("session.end"), ws.close):
                with contextlib.suppress(Exception):
                    shutdown()


# --- plumbing ---------------------------------------------------------------

# How long to keep refusing after the debug port has failed once. Not for a
# browser that has *gone* -- a closed port refuses instantly -- but one wedged
# and still holding it, which is what Chromium does while it thrashes on memory.
# Then every call pays the full socket timeout (5s HTTP, 15s websocket, times
# the monitor count for `screen: all`), a controller polling /v1/status every
# 15s stacks them faster than they drain, and FastAPI's 40-thread pool runs out
# -- a wedged browser taking the *agent* down with it, which is the one thing
# the degraded-boot design exists to prevent.
#
# A fast-fail latch, not a circuit breaker: being wrong costs one round trip.
DEAD_COOLDOWN = float(os.getenv("ROOM_DEAD_COOLDOWN", "5"))
_dead_until = 0.0


def _refuse_if_dead(port: int) -> None:
    left = _dead_until - time.monotonic()
    if left > 0:
        raise OSError(f"debug port {port} did not answer moments ago; "
                      f"not trying again for {left:.0f}s")


def _mark(alive: bool) -> None:
    global _dead_until
    _dead_until = 0.0 if alive else time.monotonic() + DEAD_COOLDOWN


def _get(port: int, path: str, latch: bool = True):
    """GET the debug port. `latch=False` for callers whose job is to keep asking
    a port that is not answering yet: wait_ready() polls during a launch, and a
    fast-fail there turns a 0.3s poll into a 5s one."""
    if latch:
        _refuse_if_dead(port)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            body = json.loads(r.read())
    except OSError:
        if latch:
            _mark(alive=False)
        raise
    _mark(alive=True)       # an answer is an answer, whoever went looking for it
    return body


def _connect(ws_url: str):
    """Open a websocket, return (ws, call(method, params) -> result)."""
    # suppress_origin: Chrome rejects unexpected Origins, Firefox validates them.
    # Sending none keeps both happy.
    try:
        ws = websocket.create_connection(ws_url, timeout=15, suppress_origin=True)
    except Exception:
        # The port answered /json a moment ago or we would have no url, so a
        # websocket that will not open is the deeper kind of wedged.
        _mark(alive=False)
        raise
    seq = itertools.count(1)

    def call(method: str, params: dict | None = None) -> dict:
        msg_id = next(seq)
        ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == msg_id:  # ponytail: drop events, we only await replies
                break
        if "error" in msg:
            raise RuntimeError(f"{method} failed: {msg['error']}")
        return msg.get("result", {})

    return ws, call


@contextlib.contextmanager
def _rpc(ws_url: str):
    ws, call = _connect(ws_url)
    try:
        yield call
    finally:
        ws.close()


# Seconds between moving a window and fullscreening it. Override with
# ROOM_PLACE_SETTLE to test a compositor that is slower than this.
PLACE_SETTLE = float(os.getenv("ROOM_PLACE_SETTLE", "0.3"))

# screen name -> CDP target id, filled in by launch()/open_window().
_targets: dict[str, str] = {}

# Extension directories this browser was actually started with. Compared against
# what is on disk to answer "does a restart have anything to pick up?" --
# --load-extension is a launch flag, so installing one changes nothing until then.
_loaded: list[str] = []


# Targets that are type "page" in /json but can never be one of *our* windows.
# Each stray one shifts the index of everything after it, and _identify() below
# maps screens to windows by list position -- so one of these silently moved
# every screen one place along, then cached the wrong answer.
#
# Only these two, and only because a kiosk window provably cannot show them:
# /v1/navigate allows http and https alone and `home_url` is validated the same
# way (app.py `_home_url`), so nothing can steer a window here.
#
# `chrome-error://` is deliberately NOT here: that is our own window having
# failed to load, which is what /v1/inspect exists to report and update.sh rolls
# back on. `chrome://` is out for a weaker version of the same reason -- a
# crash-restored new-tab page can be a real window.
_NOT_OURS = ("devtools://", "chrome-extension://")


def _pages(port: int) -> list[dict]:
    """The kiosk's own content targets, in the browser's own order."""
    return [t for t in _get(port, "/json")
            if t.get("type") == "page"
            and not str(t.get("url") or "").startswith(_NOT_OURS)]


def _cdp_page(cfg: dict, screen: str | None = None) -> dict:
    """The page target belonging to `screen`."""
    pages = _pages(cfg["browser"]["debug_port"])
    if not pages:
        raise RuntimeError("browser has no page target")

    scr = screens(cfg)
    name = screen or scr[0]["name"]
    tid = _targets.get(name)
    for p in pages:
        if p["id"] == tid:
            return p

    page = _identify(cfg, name, scr, pages)
    _targets[name] = page["id"]
    return page


def _identify(cfg: dict, name: str, scr: list[dict], pages: list[dict]) -> dict:
    """Which window belongs to `name`, when there is no mapping yet.

    Reached on the first call after a launch, after a window is closed and
    reopened, and whenever we are driving a browser we did not start.

    List order is the order the windows were opened in, so it is the answer
    exactly while there are as many windows as screens. When there are not,
    position means nothing, and guessing sends the next click -- or the next
    typed password -- to whichever monitor sorted into that slot.
    """
    names = [s["name"] for s in scr]
    i = names.index(name) if name in names else 0
    if len(scr) == 1:
        return pages[0]                     # nothing to get wrong
    if len(pages) == len(scr):
        return pages[i]

    # Ask the browser where its windows actually are, and match that against the
    # screen's own coordinates -- the only thing here that is genuinely about
    # *which monitor*, rather than about the order of a list.
    want = (scr[i].get("position") or "").strip()
    if not want:
        raise RuntimeError(
            f"{len(pages)} windows for {len(scr)} screens, and {name!r} has no "
            f"position to identify it by; restart the agent to reopen its windows")
    x, y = _pair(want, ",", "position")
    port = cfg["browser"]["debug_port"]
    placed = []
    with _rpc(_get(port, "/json/version")["webSocketDebuggerUrl"]) as call:
        for p in pages:
            with contextlib.suppress(Exception):    # a target that just closed
                b = call("Browser.getWindowForTarget",
                         {"targetId": p["id"]}).get("bounds") or {}
                if "left" in b and "top" in b:
                    placed.append((abs(b["left"] - x) + abs(b["top"] - y), p))
    if not placed:
        raise RuntimeError(
            f"{len(pages)} windows for {len(scr)} screens and none would say "
            f"where it is; cannot tell which one is {name!r}")
    # Nearest, not exact: a compositor is entitled to adjust a window by a few
    # pixels, and the nearest window to where this screen is supposed to be
    # beats the nth entry of a list that no longer lines up.
    placed.sort(key=lambda t: t[0])
    off, page = placed[0]
    log.warning("screen %r: %d windows for %d screens, matched %s by position "
                "(%d px from %s)", name, len(pages), len(scr), page["id"], off, want)
    return page


def _bidi_url(port: int) -> str:
    # Firefox >= 129 is BiDi-only: no /json discovery, the endpoint is fixed.
    return f"ws://127.0.0.1:{port}/session"


# Firefox allows ONE BiDi session per browser and does not end it when the socket
# closes, so a session per call fails from the second call on. Hold the socket.
_bidi_conns: dict[int, tuple] = {}

# ...and it is *one* socket, shared: every browser route is `def`, so two
# Firefox requests would send and recv() on it at once, and the id-matching loop
# in _connect() means one thread eats the other's reply while the loser blocks
# to its 15s timeout. Two could also race session.new and leak the losing
# socket, which is the one Firefox will not replace. Serialised instead -- BiDi
# here is the dev box, where one request at a time costs nothing.
_bidi_lock = threading.Lock()


def _bidi(port: int, method: str, params: dict | None = None) -> dict:
    """Call a BiDi method on the long-lived session, reconnecting once if stale."""
    with _bidi_lock:
        for final in (False, True):
            ws, call = _bidi_conns.get(port) or _bidi_connect(port)
            try:
                return call(method, params)
            except (OSError, websocket.WebSocketException):
                _bidi_conns.pop(port, None)
                with contextlib.suppress(Exception):
                    ws.close()
                if final:
                    raise


def _bidi_connect(port: int) -> tuple:
    # Callers hold _bidi_lock; close() is the only other toucher and takes it too.
    ws, call = _connect(_bidi_url(port))
    try:
        # ponytail: if this says "session not created", a previous agent died
        # holding the session — restart the browser. Stealing it needs a session
        # id we never saw.
        call("session.new", {"capabilities": {}})
    except Exception:
        ws.close()
        raise
    _bidi_conns[port] = (ws, call)
    return _bidi_conns[port]


def _top_context(port: int) -> dict:
    return _bidi(port, "browsingContext.getTree")["contexts"][0]
