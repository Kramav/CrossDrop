"""The browser extension (extension/).

    pytest tests/test_extension.py                                       # static only
    CROSSDROP_SMOKE=1 CROSSDROP_BROWSER=chromium pytest tests/test_extension.py

Without CROSSDROP_SMOKE only the manifest is checked. With it, a headless
chromium or edge loads the extension and its service worker is driven over CDP
against a real agent whose browser.navigate only records. Everything between
the click and that call is real: the permission check, the fetch, the
Authorization header, the agent's routing and auth. The claims M1 rested on,
none of which a stub can make:

  - **No CORS middleware is needed.** A service worker holding a host
    permission reaches the agent with no preflight; the agent 405s OPTIONS.
  - **A missing permission is named, not reported as unreachable.** Without it
    the fetch fails exactly like a box that is off.
  - **The Freethrow bridge PUTs each window's active tab** to 127.0.0.1, with a
    chrome-extension:// Origin -- what Freethrow's listener uses to refuse web
    pages, which cannot forge one.

The extension is loaded with CDP's Extensions.loadUnpacked, because branded
Chrome has ignored --load-extension since 137.
"""

import contextlib
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import browser

EXT = Path(__file__).resolve().parents[1] / "extension"
TOKEN = "test-token"

smoke = pytest.mark.skipif(
    not os.getenv("CROSSDROP_SMOKE"), reason="set CROSSDROP_SMOKE=1 to drive a real browser")


def manifest() -> dict:
    return json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))


def test_manifest_asks_for_nothing_at_install():
    """No install-time warning. Site access is requested per display when it is
    saved; a host_permissions entry would be "read and change all your data on
    all websites", because a match pattern cannot say 100.64.0.0/10."""
    m = manifest()
    assert m["manifest_version"] == 3
    assert "host_permissions" not in m
    # "tabs" is the browsing-history warning. Only the Freethrow switch needs it.
    assert "tabs" not in m["permissions"]
    assert "tabs" in m["optional_permissions"]


def test_every_referenced_file_exists():
    m = manifest()
    referenced = [m["background"]["service_worker"], m["action"]["default_popup"]]
    referenced += re.findall(r'importScripts\("([^"]+)"\)', (EXT / "background.js").read_text(encoding="utf-8"))
    referenced += re.findall(r'<script src="([^"]+)"', (EXT / "popup.html").read_text(encoding="utf-8"))
    assert len(referenced) >= 4, referenced
    for f in referenced:
        assert (EXT / f).is_file(), f"{f} is referenced but missing"


# --- real browser -------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(get, secs: float, why: str):
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if value := get():
            return value
        time.sleep(0.2)
    pytest.fail(why)


def serve(handler, port: int = 0):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture(scope="module")
def agent(tmp_path_factory):
    """A real agent on a real port. Its browser is never launched; navigate
    records the url instead, so nothing here needs a kiosk."""
    import uvicorn

    from agent.app import app

    tmp = tmp_path_factory.mktemp("agent")
    cfg = tmp / "config.toml"
    cfg.write_text(f'token = "{TOKEN}"\nhome_url = "about:blank"\n'
                   f'[browser]\nkind = "chromium"\nautolaunch = false\n', encoding="utf-8")
    sent: list[str] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CROSSDROP_CONFIG", str(cfg))
        mp.setenv("CROSSDROP_SETTINGS", str(tmp / "settings.json"))
        mp.setattr(browser, "navigate", lambda cfg, url, screen=None: sent.append(url) or url)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        wait_for(lambda: server.started, 30, "agent never started")
        port = server.servers[0].sockets[0].getsockname()[1]
        yield SimpleNamespace(url=f"http://127.0.0.1:{port}", sent=sent)
        server.should_exit = True
        thread.join(30)


@contextlib.contextmanager
def extension_browser(ext_dir: Path, profile: Path):
    kind = os.getenv("CROSSDROP_BROWSER", "chromium")
    if kind == "firefox":
        pytest.skip("the extension is Chrome/Edge MV3; set CROSSDROP_BROWSER=chromium or edge")
    try:
        exe = browser._exe(kind)
    except RuntimeError as e:
        pytest.skip(str(e))

    port = free_port()
    proc = subprocess.Popen(
        [exe, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check", "--enable-unsafe-extension-debugging",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=os.name != "nt")
    ws = None
    try:
        browser.wait_ready(kind, port, timeout=120)     # CI's chromium is slow to open its port
        version = browser._get(port, "/json/version", latch=False)
        with browser._rpc(version["webSocketDebuggerUrl"]) as call:
            ext_id = call("Extensions.loadUnpacked", {"path": str(ext_dir)})["id"]

        def worker():
            return next((t for t in browser._get(port, "/json/list", latch=False)
                         if t["type"] == "service_worker"
                         and t["url"].startswith(f"chrome-extension://{ext_id}/")), None)

        sw = wait_for(worker, 30, "the extension's service worker never started")
        # Held open for the whole module: an attached debugger keeps the service
        # worker alive, so it cannot be torn down between two tests.
        ws, sw_call = browser._connect(sw["webSocketDebuggerUrl"])

        def js(expression: str):
            r = sw_call("Runtime.evaluate", {"expression": expression,
                                             "awaitPromise": True, "returnByValue": True})
            if "exceptionDetails" in r:
                raise AssertionError(r["exceptionDetails"])
            return r["result"].get("value")

        # The target is listed before its script has run; evaluating then fails
        # with "chrome is not defined". Wait for the worker's own globals.
        def ready():
            with contextlib.suppress(AssertionError):
                return js("typeof chrome === 'object' && typeof send === 'function'")

        wait_for(ready, 15, "the service worker never finished loading background.js")

        def open_tab(url: str):
            with browser._rpc(version["webSocketDebuggerUrl"]) as call:
                call("Target.createTarget", {"url": url})

        yield SimpleNamespace(js=js, open_tab=open_tab)
    finally:
        if ws:
            ws.close()
        # Kills the tree: a launcher pid alone leaves headless children holding
        # the port, and the next launch then silently talks to *them*.
        browser.stop({"browser": {"kind": kind, "debug_port": port}}, proc)


@pytest.fixture(scope="module")
def granted(tmp_path_factory):
    """The extension with its optional permissions already granted. Granting at
    runtime needs a click on a browser prompt a headless test cannot press, and
    a granted optional permission is the same grant as a manifest one."""
    tmp = tmp_path_factory.mktemp("granted")
    ext = shutil.copytree(EXT, tmp / "extension")
    m = manifest()
    m["host_permissions"] = ["http://127.0.0.1/*"]
    m["permissions"].append("tabs")
    (ext / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    with extension_browser(ext, tmp / "profile") as b:
        yield b


@pytest.fixture(scope="module")
def ungranted(tmp_path_factory):
    """The extension exactly as shipped: nothing granted yet."""
    with extension_browser(EXT, tmp_path_factory.mktemp("ungranted") / "profile") as b:
        yield b


def use_display(b, url: str, token: str = TOKEN):
    b.js("chrome.storage.sync.set({devices: [{name: 'Test wall', "
         f"url: {json.dumps(url)}, token: {json.dumps(token)}}}]}})")


@smoke
def test_smoke_send_reaches_the_agent(granted, agent):
    use_display(granted, agent.url)
    before = len(agent.sent)
    r = granted.js('send("https://example.com/slides")')
    assert r == {"ok": True, "message": "Sent to Test wall."}, r
    assert agent.sent[before:] == ["https://example.com/slides"]
    assert granted.js("chrome.action.getBadgeText({})") == "✓"


@smoke
def test_smoke_check_reads_status(granted, agent):
    """The agent's browser is down in this fixture, and `up` is always true --
    so this passes only if the popup reads `browser`, the field with the news."""
    use_display(granted, agent.url)
    r = granted.js("check()")
    assert r["ok"] is False and "browser isn't" in r["message"], r


@smoke
def test_smoke_failures_say_why(granted, agent):
    use_display(granted, agent.url, token="wrong")
    assert "rejected the token" in granted.js('send("https://example.com/")')["message"]
    assert granted.js("chrome.action.getBadgeText({})") == "!"

    use_display(granted, f"http://127.0.0.1:{free_port()}")    # nothing listening
    assert "Can't reach Test wall" in granted.js('send("https://example.com/")')["message"]

    use_display(granted, agent.url)
    assert "Only http and https" in granted.js('send("chrome://settings")')["message"]


@smoke
def test_smoke_no_permission_is_named_not_unreachable(ungranted, agent):
    use_display(ungranted, agent.url)
    before = len(agent.sent)
    r = ungranted.js('send("https://example.com/")')
    assert r["ok"] is False and "press Save" in r["message"], r
    assert agent.sent[before:] == []


@smoke
def test_smoke_freethrow_bridge_reports_each_window(granted):
    port = int(re.search(r"127\.0\.0\.1:(\d+)", (EXT / "freethrow.js").read_text(encoding="utf-8"))[1])
    got: list[tuple[str, dict, dict]] = []

    class Freethrow(http.server.BaseHTTPRequestHandler):
        def do_PUT(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            got.append((self.path, dict(self.headers), body))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    class Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            html = b"<!doctype html><title>Freethrow fixture</title>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *a):
            pass

    try:
        listener = serve(Freethrow, port)
    except OSError:
        pytest.skip(f"port {port} is taken -- probably Freethrow itself")
    page = serve(Page)
    url = f"http://127.0.0.1:{page.server_port}/"
    try:
        granted.js("chrome.storage.local.set({freethrow: true})")
        granted.open_tab(url)

        def seen():
            return next((w for _, _, b in got for w in b["windows"]
                         if w["url"] == url and w["title"] == "Freethrow fixture"), None)

        window = wait_for(seen, 15, f"no PUT ever named {url}; got {got}")
        path, headers, body = got[-1]
        assert path == "/crossdrop/windows"
        assert body["v"] == 1 and body["browser"] in ("chrome", "edge")
        assert len(body["instance"]) == 36          # a UUID: one snapshot per profile
        assert {"id", "focused", "state", "left", "top", "width", "height"} <= window.keys()
        headers = {k.lower(): v for k, v in headers.items()}
        assert headers["content-type"] == "application/json"
        # What Freethrow's listener checks. A PUT always carries an Origin, and a
        # web page cannot forge this one.
        assert headers["origin"].startswith("chrome-extension://"), headers
    finally:
        granted.js("chrome.storage.local.set({freethrow: false})")
        listener.shutdown()
        page.shutdown()
