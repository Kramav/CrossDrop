"""Launch a kiosk browser and drive its tab.

Two protocols, because Firefox dropped CDP in 129 (we're on 140):
  - chromium/edge -> CDP        (Page.navigate)
  - firefox       -> WebDriver BiDi (browsingContext.navigate)
Both are JSON-RPC over one websocket, so `_rpc` serves both.
"""

import contextlib
import itertools
import json
import os
import shutil
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

import websocket

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
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ],
}


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


def launch(cfg: dict) -> subprocess.Popen:
    """Start the kiosk browser and block until its debug port answers."""
    b = cfg["browser"]
    kind, port = b["kind"], b["debug_port"]
    profile = Path(b["profile_dir"])
    profile.mkdir(parents=True, exist_ok=True)
    home = cfg["home_url"]

    if kind == "firefox":
        argv = [_exe(kind, b["path"]), "--remote-debugging-port", str(port),
                "--profile", str(profile), "--no-remote", "--kiosk", home]
    else:
        argv = [_exe(kind, b["path"]), f"--remote-debugging-port={port}",
                # Chrome >= 111 rejects CDP websockets carrying an Origin header.
                # We also suppress Origin client-side, but keep this for parity
                # with the Pi's Chromium.
                "--remote-allow-origins=*",
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
                "--no-first-run", "--no-default-browser-check", home]

    # own process group on POSIX so stop() can take the whole tree down
    proc = subprocess.Popen(argv, start_new_session=os.name != "nt")
    wait_ready(kind, port)
    return proc


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
                _get(port, "/json/version")
            return
        except (OSError, websocket.WebSocketException):
            if time.monotonic() > deadline:
                raise RuntimeError(f"debug port {port} never came up")
            time.sleep(0.3)


def navigate(cfg: dict, url: str) -> str:
    """Point the kiosk tab at `url`. Returns the url we sent it to."""
    b = cfg["browser"]
    if b["kind"] == "firefox":
        port = b["debug_port"]
        # "interactive", not "none": we want /v1/navigate to mean the page really
        # committed, and it matches CDP's Page.navigate, which returns after commit.
        # A page slower than the 15s socket timeout surfaces as a 503.
        _bidi(port, "browsingContext.navigate",
              {"context": _top_context(port)["context"], "url": url,
               "wait": "interactive"})
    else:
        page = _cdp_page(b["debug_port"])
        with _rpc(page["webSocketDebuggerUrl"]) as call:
            call("Page.navigate", {"url": url})
    return url


def current_url(cfg: dict) -> str:
    b = cfg["browser"]
    if b["kind"] == "firefox":
        return _top_context(b["debug_port"])["url"]
    # CDP reports a blank tab as "", BiDi as "about:blank". Normalise, or the two
    # backends disagree and /v1/reload tries to navigate to the empty string.
    return _cdp_page(b["debug_port"])["url"] or "about:blank"


def close() -> None:
    """Release BiDi sessions. A browser we didn't launch outlives the agent, and
    Firefox won't hand out a second session while the first is open."""
    for port, (ws, call) in list(_bidi_conns.items()):
        del _bidi_conns[port]
        for shutdown in (lambda: call("session.end"), ws.close):
            with contextlib.suppress(Exception):
                shutdown()


# --- plumbing ---------------------------------------------------------------

def _get(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read())


def _connect(ws_url: str):
    """Open a websocket, return (ws, call(method, params) -> result)."""
    # suppress_origin: Chrome rejects unexpected Origins, Firefox validates them.
    # Sending none keeps both happy.
    ws = websocket.create_connection(ws_url, timeout=15, suppress_origin=True)
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


def _cdp_page(port: int) -> dict:
    pages = [t for t in _get(port, "/json") if t.get("type") == "page"]
    if not pages:
        raise RuntimeError("browser has no page target")
    return pages[0]


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
