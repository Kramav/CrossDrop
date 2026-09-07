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

# A child of app.py's "room" logger, so it inherits the level ROOM_LOG sets and
# lands in the same journal. The print()s elsewhere in this file predate that and
# are startup notices rather than diagnostics; this one is something you go
# looking for after a screen has behaved oddly.
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


# What each backend can actually do, reported by /v1/status as `supports`. A
# program checks this once instead of discovering the shape of the API by
# collecting 501s -- and the dev box ships firefox while the Pi ships chromium,
# so the two really do expose different APIs.
#
# Every name absent here has a matching NotImplementedError below; keep the two
# in step. Routes that never touch the browser (display, upload) are not listed:
# they work on every backend, so there is nothing to check.
_CDP_ONLY = ("scroll", "autoscroll", "media", "screens", "window", "extensions",
             "screenshot", "inspect", "input")
SUPPORTS = {
    "chromium": ("navigate", *_CDP_ONLY),
    "edge": ("navigate", *_CDP_ONLY),
    # Firefox: BiDi gives us navigation, and one session for the whole browser
    # (see _bidi_conns), so there is no second window to address.
    "firefox": ("navigate",),
}


def interactive(cfg: dict) -> bool:
    """Is `POST /v1/input` switched on? Off unless config.toml says otherwise.

    It is the only route that acts *as* whoever the kiosk is logged in as, so it
    is the one thing here that ships off. Install-time and file-only, in the
    config the agent cannot write (root:<user> 640) -- a switch the API can turn
    on for itself is not a switch.
    """
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

    One browser, one profile, one debug port -- and one window per screen. Two
    browser instances would double the RAM on a tmpfs profile and, worse, split
    your logins across two cookie stores (PLAN.md §6 SSO note).
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
                # websockets carrying an Origin header specifically to stop page
                # content reaching the debug port — and the page here is
                # arbitrary, by design. We send no Origin at all (_rpc passes
                # suppress_origin=True unconditionally), so the flag bought
                # nothing and disabled that defense for every rendered page.
                f"--user-data-dir={profile}", "--kiosk",
                # Never touch the system keyring. Chromium's default on Linux is
                # libsecret, and under desktop autologin the login keyring is
                # locked (nobody typed a password), so it puts up a modal unlock
                # dialog over the kiosk — on a Pi with no keyboard, forever.
                # "basic" is Chromium's own store; ignored on Windows.
                "--password-store=basic",
                # Same class of problem: after a power cut Chromium offers to
                # restore the last session in a bubble nobody can dismiss.
                "--disable-session-crashed-bubble",
                # Phase 6 puts the profile on tmpfs, so this cache is RAM the Pi
                # cannot get back. Uncapped, Chromium sizes it from free space
                # and eventually fills /run/user/<uid>, taking the kiosk with it.
                f"--disk-cache-size={b['disk_cache_mb'] * 1024 * 1024}",
                # A pushed video should start playing. Chromium's default blocks
                # autoplay with sound until someone clicks, and nobody can click
                # this box. /v1/media sends userGesture anyway, so this only
                # covers the case of arriving on a page that autoplays.
                "--autoplay-policy=no-user-gesture-required",
                "--no-first-run", "--no-default-browser-check"]
        # Unpacked extensions, because the kiosk has no UI to install one through
        # and Debian's Chromium ignores ExtensionInstallForcelist (deploy/pi/
        # README.md §10). A directory, not a list of paths, so installing one is
        # a file operation and never a config edit -- see install-extension.sh.
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
        # Wayland gives the compositor final say on window position and Chromium
        # ignores --window-position there. Under XWayland the move is an X11
        # configure request, which labwc honours. See deploy/pi/README.md.
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
        # Window 1 exists already (--kiosk put it wherever the compositor liked),
        # so it is *moved* rather than opened. Without this its `position` would
        # silently do nothing and the only way to choose its monitor would be to
        # reorder the config until it guessed right.
        place(cfg, scr[0])
    for s in scr[1:]:
        open_window(cfg, s)
    return proc


def _pair(value: str, sep: str, field: str) -> tuple[int, int]:
    """Parse "1366,0" / "2560x1440". A typo here breaks the kiosk at boot on a
    box with no keyboard, so it fails with the offending value, not a ValueError
    from deep inside a generator."""
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
    # Sized to the monitor when we know it: the window is briefly visible between
    # the move and the fullscreen, and an 800x600 box on a 1440p panel is a
    # conspicuous flash at every boot. Cosmetic only -- fullscreen overrides it.
    w, h = _pair(size, "x", "size") if size else (800, 600)
    win = call("Browser.getWindowForTarget", {"targetId": target_id})["windowId"]
    # Move first, fullscreen second: Chromium refuses to move a window that is
    # already fullscreen, so the order here is the whole trick. Setting "normal"
    # is also what un-fullscreens a --kiosk window so it *can* be moved.
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
    _require_cdp(cfg)
    port = cfg["browser"]["debug_port"]
    page = _cdp_page(cfg, screen["name"])
    with _rpc(_get(port, "/json/version")["webSocketDebuggerUrl"]) as call:
        _place(call, page["id"], screen["position"], screen.get("size", ""))
    return page["id"]


WINDOW_STATES = ("normal", "minimized", "fullscreen")


def window(cfg: dict, screen: dict, state: str) -> str:
    """Put one screen's kiosk window aside, or back.

    The escape hatch from --kiosk: with the window minimized the Pi's own
    desktop is reachable without stopping the agent, which is otherwise the
    only way in and costs you the session.

    "fullscreen" goes via "normal" for the same reason _place() does --
    Chromium will not transition straight out of minimized, and a --kiosk
    window has to be un-fullscreened before its bounds can change.

    ponytail: fire-and-forget, the agent does not track where the window went.
    So a navigate to a minimized window renders offscreen until someone asks
    for fullscreen again (or the nightly restart does). Read windowState back
    out of Browser.getWindowForTarget and carry it in ScreenOut if that ever
    needs to be visible -- it costs a CDP roundtrip on every status poll.
    """
    _require_cdp(cfg)
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
    _require_cdp(cfg)
    port = cfg["browser"]["debug_port"]
    with _rpc(_get(port, "/json/version")["webSocketDebuggerUrl"]) as call:
        tid = call("Target.createTarget",
                   {"url": screen["home_url"], "newWindow": True})["targetId"]
        _place(call, tid, screen["position"], screen.get("size", ""))
    _targets[screen["name"]] = tid
    return tid


def _require_cdp(cfg: dict) -> None:
    if cfg["browser"]["kind"] == "firefox":
        raise NotImplementedError(
            "multiple screens need CDP; use kind = \"chromium\" or \"edge\"")


def _require_addressable(cfg: dict, screen: str | None) -> None:
    """Refuse a screen Firefox cannot actually reach.

    One BiDi session per browser means one addressable window, so the `screen`
    argument has nowhere to go on this backend. Silently driving the first
    monitor instead is the worst available answer: a caller asking for `all`
    gets two successes and one changed monitor, and nothing anywhere says so.
    Fail, and let it surface as a 501 the same way scroll and media do.
    """
    first = screens(cfg)[0]["name"]
    if screen and screen != first:
        raise NotImplementedError(
            f"screen {screen!r} needs CDP; firefox drives only {first!r}")


def stop(cfg: dict, proc: subprocess.Popen) -> None:
    """Shut the browser down *and its children*. Both Firefox and Chromium fork a
    process tree; terminating the launcher alone leaves a fullscreen kiosk on
    screen and the debug port held — on a box with no keyboard, forever.

    Ask over the debug protocol first, because the pid is not a reliable handle:
    on Windows the msedge/chrome launcher exits the moment it hands off (poll()
    returns 0 with the browser very much alive), so `taskkill /T` walks a tree
    that is no longer rooted at that pid and every process survives.
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
    """True once the debug port stops answering — the browser is really down.
    The port, not the pid, is what the next agent start collides with."""
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
        # "interactive", not "none": we want /v1/navigate to mean the page really
        # committed, and it matches CDP's Page.navigate, which returns after commit.
        # A page slower than the 15s socket timeout surfaces as a 503.
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

    Synthesised as a real wheel event, not `window.scrollBy`: Chromium's PDF
    viewer is a plugin that ignores scripted window scrolling, and showing a PDF
    is half of what this display is for.
    """
    if cfg["browser"]["kind"] == "firefox":
        raise NotImplementedError(
            "scroll needs CDP; use kind = \"chromium\" or \"edge\"")
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

# Seconds of scrolling per synthesised gesture. The whole trade: longer means
# fewer round-trips, shorter means `stop` bites sooner, because a gesture runs
# to completion before we look at the event again. Override with
# ROOM_GESTURE_SECS — how smooth this looks is a property of the panel and the
# GPU, not of the code, so it is worth being able to tune without a deploy.
GESTURE_SECS = float(os.getenv("ROOM_GESTURE_SECS", "0.5"))


def autoscroll(cfg: dict, screen: str | None, speed: int,
               stop: threading.Event) -> None:
    """Scroll one screen `speed` pixels a tick until `stop` is set.

    Smoothness is the point. Ten wheel events a second is ten visible steps a
    second, and shrinking the step only trades stepping for ten times the
    round-trips on a Pi that is already rendering the page being scrolled.
    Input.synthesizeScrollGesture hands the whole movement to Chromium, which
    interpolates it at the compositor's frame rate — smoother *and* cheaper, two
    calls a second instead of ten.

    `speed` still means pixels per AUTOSCROLL_TICK, so the CLI flag and the web
    UI slider keep the values they always had; the gesture API is told pixels
    per second.

    One connection for the whole run, not one per tick (PLAN.md §11 finding 5).
    The viewport centre is read once for the same reason: a fullscreen kiosk
    window does not resize.

    A window that closes under us kills the socket and ends the run, where the
    old shape would have reconnected to whatever target replaced it. That is the
    better answer — the only things that change a target are a navigation, which
    already stops autoscroll deliberately, and the window going away.
    """
    if cfg["browser"]["kind"] == "firefox":
        raise NotImplementedError(
            "autoscroll needs CDP; use kind = \"chromium\" or \"edge\"")
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
                    # The gesture API is experimental; an old build may not have
                    # it. Drop to wheel ticks for the rest of the run rather
                    # than ending an autoscroll someone asked for.
                    print(f"autoscroll: no smooth gesture ({e}); "
                          f"falling back to wheel ticks", flush=True)
                    smooth = False
                    continue
                # The call returns when the gesture finishes, so normally there
                # is nothing left to wait for. If a target ignored it and
                # answered instantly, this is what stops the loop spinning at
                # 100% CPU on the Pi.
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
# Click and type, for the one thing this display genuinely cannot do otherwise:
# the Pi has no keyboard, so an expired SSO login or a consent wall is a page
# nobody can get past (PLAN.md §6 "SSO expiry").
#
# Everything here is dispatched into one CDP *target* -- the same connection
# navigate uses. It reaches that window's renderer and nothing else: it cannot
# alt-tab, cannot reach the window manager, cannot close the kiosk, and cannot
# type into any other application. That is the boundary, and it is a property of
# the transport rather than a rule anyone has to remember.

INPUT_ACTIONS = ("click", "double", "right", "move", "drag", "type", "key", "wait")

# Modifier bits, as CDP wants them.
_MODIFIERS = {"alt": 1, "ctrl": 2, "control": 2, "meta": 4, "cmd": 4, "shift": 8}

# The named keys worth having: everything a login form or a PDF viewer needs.
# name -> (windowsVirtualKeyCode, key, text). A single printable character is
# handled separately; anything else is refused rather than guessed at, because a
# key that silently does nothing is worse than one that says it is unsupported.
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

# Find an element and give back where to click it. scrollIntoView first: an
# element below the fold has coordinates outside the viewport, and dispatching a
# click at those lands on nothing at all while reporting success.
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

    Run over the whole list before any of it executes, because there is no undo:
    a typo in action 3 must not be discovered after actions 1 and 2 have already
    clicked something and typed into it. `/v1/extensions` sets the precedent --
    the caller's typo is total, a runtime failure is per-item.
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
        # Intermediate moves, not just press-then-release: HTML5 drag and drop
        # and every canvas app want to see the pointer travel, and a single jump
        # is routinely ignored as a stray click.
        for i in range(1, 6):
            _mouse(call, "mouseMoved", x0 + (x1 - x0) * i // 5,
                   y0 + (y1 - y0) * i // 5)
        _mouse(call, "mouseReleased", x1, y1)
        return {"x": x1, "y": y1}

    if do == "type":
        # insertText, not a key event per character: one round trip instead of
        # two per letter, and it still fires beforeinput/input, which is what a
        # framework-controlled field actually listens for.
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

    A list rather than a route per verb: a login is click, type, click, type,
    click, and as five requests that is five websockets to the debug port and
    five chances to interleave with something else. One request is one
    connection, one ordering, and one audit record.

    **Stops at the first failure.** Half a login sequence is the dangerous case:
    if the click that focuses the password box missed, the next action would
    type the password into whatever does have focus. The result list says which
    action stopped it.

    `deadline` is a time.monotonic() value. Checked between actions, so a long
    list cannot hold a threadpool thread indefinitely.
    """
    if cfg["browser"]["kind"] == "firefox":
        # BiDi has input.performActions, so this is unwritten rather than
        # impossible -- but the Pi runs chromium and the dev box is the only
        # firefox. Same 501 as scroll and media.
        raise NotImplementedError(
            "input needs CDP; use kind = \"chromium\" or \"edge\"")
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

# Whatever the page is playing, driven through the element itself — there is no
# CDP "media" domain to ask, and every player worth showing on a wall is an
# HTML5 <video>/<audio> underneath its own controls.
#
# ponytail: top frame, main world, no shadow DOM. A site that embeds its player
# in a cross-origin <iframe> (an embedded YouTube, not youtube.com itself) has no
# media element here and reports nothing playing. Reaching those needs a per-frame
# execution context, i.e. Runtime.enable and event handling in _connect().
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
    if cfg["browser"]["kind"] == "firefox":
        raise NotImplementedError(
            "media control needs CDP; use kind = \"chromium\" or \"edge\"")
    if action not in MEDIA_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(MEDIA_ACTIONS)}")
    page = _cdp_page(cfg, screen)
    with _rpc(page["webSocketDebuggerUrl"]) as call:
        return _evaluate(call, _MEDIA_JS % {"action": json.dumps(action),
                                            "value": json.dumps(float(value))},
                         f"media {action}")


def _viewport(call) -> tuple[int, int]:
    """The window's CSS-pixel viewport. The fallback is a guess, and is only
    ever better than nothing: 800x600 keeps a scroll off the PDF sidebar and
    gives a screenshot a plausible clip rather than an exception."""
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

    Exists because /v1/navigate reports the url we *sent*: a redirect, a login
    wall, a consent banner or an "Aw, Snap!" all look like success from every
    other route in this API. This is the read-back that a url cannot be.

    The clip is always sent, always at `scale: 1`, and that is the whole
    coordinate contract: without a clip Chromium captures at the device pixel
    ratio, so a 1920-wide viewport on a HiDPI panel comes back 3840 wide and
    anything mapping image pixels back onto the page is off by a factor of two.
    Pinned this way, **image pixels are CSS pixels** and the returned
    width/height are the same space Input.dispatchMouseEvent takes.

    `region` is clamped to the viewport rather than rejected — an off-by-a-bit
    rect is worth a slightly smaller picture, not a 422 — so the returned
    width/height are what you got, which need not be what you asked for.

    This is a page capture, not a screen capture: it renders the frame tree of
    one browser target and can no more see the box's desktop, its other windows
    or its taskbar than Page.navigate can drive them.
    """
    if cfg["browser"]["kind"] == "firefox":
        # BiDi does have browsingContext.captureScreenshot, so this one is not
        # impossible on Firefox the way scroll and media are -- it is just not
        # written, and the Pi runs chromium. Same 501 either way.
        raise NotImplementedError(
            "screenshot needs CDP; use kind = \"chromium\" or \"edge\"")
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


# Structured page state, for a caller that cannot look at a picture. A
# screenshot answers "what is wrong" for a person; this answers it for eve, for
# update.sh, and for anything deciding whether to retry.
#
# Deliberately reports no field *values*. Naming a password box is how a caller
# knows where to type; handing back what is in it turns a diagnostic route into
# a credential leak.
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
    if cfg["browser"]["kind"] == "firefox":
        raise NotImplementedError(
            "inspect needs CDP; use kind = \"chromium\" or \"edge\"")
    page = _cdp_page(cfg, screen)
    with _rpc(page["webSocketDebuggerUrl"]) as call:
        state = _evaluate(call, _INSPECT_JS, "inspect")
    url = page.get("url") or "about:blank"
    out = {"url": url, **(state or {})}
    # A page that failed to load may have no document to ask, so the url is the
    # second witness -- Chromium parks these on chrome-error://chromewebdata/.
    out["error_page"] = bool(out.get("error_page")) or url.startswith("chrome-error")
    return out


def _evaluate(call, expression: str, what: str):
    """Run a page script and return its value, or raise with the page's own
    message. Every JS-bearing route funnels through here so a thrown TypeError
    surfaces as a failure rather than as a silent None."""
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
    """Release BiDi sessions. A browser we didn't launch outlives the agent, and
    Firefox won't hand out a second session while the first is open."""
    for port, (ws, call) in list(_bidi_conns.items()):
        del _bidi_conns[port]
        for shutdown in (lambda: call("session.end"), ws.close):
            with contextlib.suppress(Exception):
                shutdown()


# --- plumbing ---------------------------------------------------------------

# How long to keep refusing after the debug port has failed us once. The case
# this exists for is not a browser that has *gone* -- a closed port refuses
# instantly -- but one that is wedged and still holding it, which is what
# Chromium does while it thrashes on memory. Then every call pays the full
# socket timeout: 5s for the HTTP probe, 15s for a websocket, and `screen: all`
# multiplies it by the monitor count.
#
# A controller polling /v1/status every 15s stacks those up faster than they
# drain, one threadpool thread per screen per poll, and FastAPI's pool is 40 --
# so a wedged browser quietly takes the *agent* down with it, which is the one
# thing the whole degraded-boot design exists to prevent.
#
# The window is deliberately short: this is a fast-fail latch, not a circuit
# breaker with a state machine. Being wrong costs one round trip.
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
    """GET the debug port. `latch=False` for the callers whose whole job is to
    keep asking a port that is not answering yet -- wait_ready() polls during a
    launch, and a fast-fail there would turn a 0.3s poll into a 5s one and leave
    the kiosk sitting dark for five seconds after it was ready."""
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
        # The port answered /json a moment ago or we would not have a url, so a
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


# Targets that are pages but can never be one of *our* windows. Both of these
# are type "page" in /json, and each one shifts the index of everything after
# it -- which mattered because the fallback below used to map screens to windows
# by position in this list, so one stray target silently moved every screen one
# place along, then cached the wrong answer so it stayed wrong.
#
# Only these two, and only because a kiosk window provably cannot be showing
# them: /v1/navigate allows http and https alone, and `home_url` is validated
# the same way, so nothing can steer a window here.
#
# `chrome-error://` is deliberately NOT in this list, though it looks like it
# belongs: that is our own window having failed to load, which is exactly the
# state /v1/inspect exists to report and update.sh rolls a release back on.
# Filtering it would lose the window at the moment it most needs describing.
# `chrome://` is out for a weaker version of the same reason -- a crash-restored
# new-tab page can be a real window, and dropping it would strand the screen.
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

    Reached on the first call after a launch, after a window has been closed and
    reopened, and whenever we are driving a browser we did not start.

    List order is the answer only when it can be: it is the order the windows
    were opened in, so it holds exactly while there are as many windows as
    screens. When there are not, position in a list means nothing at all, and
    guessing sends the next click -- or the next typed password -- to whichever
    monitor happened to sort into that slot.
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


def _bidi(port: int, method: str, params: dict | None = None) -> dict:
    """Call a BiDi method on the long-lived session, reconnecting once if stale."""
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
