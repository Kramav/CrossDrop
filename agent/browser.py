"""Launch a kiosk browser and drive its tab.

Two protocols, because Firefox dropped CDP in 129 (we're on 140):
  - chromium/edge -> CDP        (Page.navigate)
  - firefox       -> WebDriver BiDi (browsingContext.navigate)
Both are JSON-RPC over one websocket, so `_rpc` serves both.
"""

import contextlib
import itertools
import json
import shutil
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
                "--no-first-run", "--no-default-browser-check", home]

    proc = subprocess.Popen(argv)
    wait_ready(kind, port)
    return proc


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
    """Point the kiosk tab at `url`. Returns the tab's url afterwards."""
    b = cfg["browser"]
    if b["kind"] == "firefox":
        with _bidi(b["debug_port"]) as (call, ctx):
            call("browsingContext.navigate",
                 {"context": ctx["context"], "url": url, "wait": "none"})
    else:
        page = _cdp_page(b["debug_port"])
        with _rpc(page["webSocketDebuggerUrl"]) as call:
            call("Page.navigate", {"url": url})
    return url


def current_url(cfg: dict) -> str:
    b = cfg["browser"]
    if b["kind"] == "firefox":
        with _bidi(b["debug_port"]) as (_call, ctx):
            return ctx["url"]
    return _cdp_page(b["debug_port"])["url"]


# --- plumbing ---------------------------------------------------------------

def _get(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read())


@contextlib.contextmanager
def _rpc(ws_url: str):
    """Yield call(method, params) -> result over one websocket."""
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


@contextlib.contextmanager
def _bidi(port: int):
    """Yield (call, top-level browsing context) for a fresh BiDi session."""
    with _rpc(_bidi_url(port)) as call:
        call("session.new", {"capabilities": {}})
        yield call, call("browsingContext.getTree", {})["contexts"][0]
