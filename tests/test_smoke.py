"""Real-browser smoke tests — the things a stub cannot prove.

    CROSSDROP_BROWSER=chromium CROSSDROP_SMOKE=1 pytest tests/test_smoke.py -s

Skipped without both. Everything here is CDP-only, so it needs chromium or edge;
on the Pi that is what runs anyway, and this file is the closest thing to an
acceptance test for `screenshot`, `inspect` and `input`.

The three claims these exist to check, none of which survives being stubbed:

  - **A capture is in CSS pixels.** Ask for a 200x100 region and the PNG header
    has to say 200x100. On a HiDPI panel an unpinned capture comes back doubled,
    every coordinate a client derives from it is then off by a factor of two,
    and nothing else in the suite would notice.
  - **`error_page` is real.** update.sh rolls a release back on it, and until
    now it had only ever been true in a fixture.
  - **A click lands where it was aimed.** Proved by the page changing, not by
    anyone looking at it: a click at the wrong coordinates hits nothing and the
    DOM stays put.
"""

import http.server
import os
import struct
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("CROSSDROP_SMOKE"), reason="set CROSSDROP_SMOKE=1 to drive a real browser")

TOKEN = "test-token"

# One page with everything worth aiming at. The button is positioned absolutely
# so a *coordinate* click has a known target -- that is the number the web UI
# computes from a screenshot, and the only way to prove it arrives is to hit
# something with it. Every handler reports through document.title, because
# /v1/inspect deliberately never returns a field's value.
FIXTURE_HTML = b"""<!doctype html>
<meta charset="utf-8"><title>ready</title>
<style>
  body { margin: 0; font: 16px sans-serif; }
  #btn { position: absolute; left: 100px; top: 50px; width: 200px; height: 100px; }
  #box { position: absolute; left: 100px; top: 200px; width: 300px; }
</style>
<button id="btn" onclick="document.title = 'clicked'">Go</button>
<input id="box" name="who" placeholder="Username"
       oninput="document.title = 'typed:' + this.value">
<form id="f" onsubmit="document.title = 'submitted'; return false">
  <input id="infield" name="pw" type="password">
</form>
"""


@pytest.fixture(scope="module")
def page_server():
    """Serve FIXTURE_HTML. /v1/navigate allows http and https only, so a data:
    url is not a way to get a test page onto the kiosk."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(FIXTURE_HTML)))
            self.end_headers()
            self.wfile.write(FIXTURE_HTML)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/"
    srv.shutdown()


@pytest.fixture(scope="module")
def kiosk(tmp_path_factory):
    """A real agent driving a real chromium, with input switched on.

    Module-scoped: one browser for the whole file rather than one per test. Each
    test navigates before it asserts, so they do not depend on each other -- and
    thirteen kiosk launches is thirteen fullscreen windows on whoever's desktop
    is running this, plus a minute of nothing but startup.
    """
    import httpx
    import uvicorn

    from agent import browser
    from agent.app import app

    kind = os.getenv("CROSSDROP_BROWSER", "chromium")
    if kind == "firefox":
        pytest.skip("these routes are CDP-only; set CROSSDROP_BROWSER=chromium")
    try:
        browser._exe(kind)
    except RuntimeError as e:
        pytest.skip(str(e))

    # Refuse to start on a debug port somebody else already holds. On the Pi
    # that is the live agent's kiosk, and without this check the failure is
    # silent and destructive: launch() cannot bind the port, wait_ready()
    # succeeds against the *running* browser instead, and every test below then
    # drives the real display -- navigating it away, clicking on it, typing into
    # it -- before teardown asserts the thing has been shut down.
    #
    # A failure, not a skip. "Your display is in the way" is something to go and
    # fix, not something to quietly not test.
    try:
        browser._get(9222, "/json/version", latch=False)
    except OSError:
        pass                                    # nothing there: ours to use
    else:
        pytest.fail("debug port 9222 is already in use — almost certainly the "
                    "live agent. Stop it first: systemctl --user stop "
                    "crossdrop-agent  (see deploy/pi/smoke-on-the-pi.md)")

    tmp = tmp_path_factory.mktemp("smoke")
    cfg = tmp / "config.toml"
    cfg.write_text(f'token = "{TOKEN}"\nhome_url = "about:blank"\n'
                   f'[browser]\nkind = "{kind}"\nautolaunch = true\n'
                   f'[interact]\nenabled = true\n', encoding="utf-8")

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CROSSDROP_CONFIG", str(cfg))
        mp.setenv("CROSSDROP_SETTINGS", str(tmp / "settings.json"))
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0,
                                               log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        while not server.started:
            time.sleep(0.1)
        port = server.servers[0].sockets[0].getsockname()[1]
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30,
                          headers={"Authorization": f"Bearer {TOKEN}"}) as c:
            # The browser comes up on a background thread now, so wait for it
            # rather than racing it: /v1/status carries `error` until it lands,
            # and reports why if it never does.
            #
            # 120s, not 30. Chromium under a bare Xvfb with no window manager,
            # no dbus and no GPU is slow and *variable* -- on a shared CI runner
            # it has taken anywhere from 15s to over 30s to open its debug port.
            # At 30s this failed while printing `'browser': 'ok'` in the very
            # status call it made to build the message, which is the signature of
            # a timeout rather than a broken browser. Waiting longer costs
            # nothing on a good run: the loop breaks as soon as it is up.
            deadline = time.monotonic() + 120
            while (last := c.get("/v1/status").json())["browser"] != "ok":
                if time.monotonic() > deadline:
                    pytest.fail(f"browser never came up in 120s: {last}")
                time.sleep(0.5)
            yield c
        server.should_exit = True
        thread.join(30)
    # A leaked kiosk keeps holding 9222 and the next agent silently drives it.
    with pytest.raises(RuntimeError):
        browser.wait_ready(kind, 9222, timeout=10)


def png_size(blob: bytes) -> tuple[int, int]:
    assert blob[:8] == b"\x89PNG\r\n\x1a\n", "not a png"
    return struct.unpack(">II", blob[16:24])     # IHDR width, height


def title_of(c) -> str:
    return c.get("/v1/inspect").json()["title"]


def wait_title(c, want: str, secs: float = 5.0) -> str:
    """Poll for a title. A click is dispatched, not awaited: the handler runs on
    the page's own turn of the event loop, not on the CDP round trip."""
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        got = title_of(c)
        if got == want:
            return got
        time.sleep(0.1)
    return title_of(c)


# --- screenshot -------------------------------------------------------------

def test_smoke_a_capture_is_in_css_pixels(kiosk, page_server):
    """The DPR contract, and the reason every clip is pinned to scale 1. Ask for
    200x100 and an unpinned capture on a HiDPI panel returns 400x200."""
    import base64
    kiosk.post("/v1/navigate", json={"url": page_server})
    r = kiosk.post("/v1/screenshot",
                   json={"region": {"x": 0, "y": 0, "width": 200, "height": 100}})
    assert r.status_code == 200, r.text
    shot = r.json()
    assert (shot["width"], shot["height"]) == (200, 100), shot
    # What the file itself says, not what we told the caller it said.
    assert png_size(base64.b64decode(shot["image"])) == (200, 100)


def test_smoke_a_full_capture_matches_what_it_reports(kiosk, page_server):
    import base64
    kiosk.post("/v1/navigate", json={"url": page_server})
    # Not immediately: Page.navigate returns on *commit*, and for a moment after
    # that the target listing still carries Chromium's provisional title, which
    # is the bare host. Waiting for the document to be parsed is what the UI's
    # settle() does for the same reason.
    assert wait_title(kiosk, "ready") == "ready"
    shot = kiosk.post("/v1/screenshot", json={}).json()
    assert png_size(base64.b64decode(shot["image"])) == (shot["width"], shot["height"])
    # A fullscreen kiosk on any real monitor. Mostly this catches the viewport
    # falling back to the 800x600 guess, which would silently halve every
    # coordinate a client derives from the picture.
    assert shot["width"] >= 640 and shot["height"] >= 480, shot
    assert shot["title"] == "ready", "screenshot reported a stale title"


def test_smoke_jpeg_decodes_and_is_the_same_size_on_screen(kiosk, page_server):
    """update.sh saves its rollback diagnostic as a jpeg, on an SD card.

    Note what is *not* asserted: that the jpeg is smaller. On a flat kiosk page
    it is routinely bigger — this fixture measures 33 KB of jpeg against 21 KB
    of png, because png compresses a white background almost perfectly while
    jpeg still pays for a colour profile and its DCT blocks. Jpeg is chosen for
    the *worst* case (a photo, a video frame) where png runs to megabytes, not
    for the average one. Believing otherwise is what this test exists to stop.
    """
    import base64
    kiosk.post("/v1/navigate", json={"url": page_server})
    png = kiosk.post("/v1/screenshot", json={"format": "png"}).json()
    jpg = kiosk.post("/v1/screenshot",
                     json={"format": "jpeg", "quality": 60}).json()
    assert base64.b64decode(jpg["image"])[:2] == b"\xff\xd8"        # SOI
    assert base64.b64decode(jpg["image"])[-2:] == b"\xff\xd9"       # EOI
    # Same picture, same coordinate space, whatever the encoding costs.
    assert (jpg["width"], jpg["height"]) == (png["width"], png["height"])


# --- inspect ----------------------------------------------------------------

def test_smoke_inspect_reads_a_real_page(kiosk, page_server):
    kiosk.post("/v1/navigate", json={"url": page_server})
    s = kiosk.get("/v1/inspect").json()
    # "interactive" or "complete", not "complete" alone. Page.navigate returns on
    # commit, so on a slow CI runner inspect can land between DOM-parsed and the
    # load event -- this failed on 2026-09-11 and 09-12 with 'interactive'. Both
    # mean the document is parsed, which is all the title and fields below need.
    # "loading" or "unknown" would mean inspect read the page too early; still a failure.
    assert s["title"] == "ready" and s["ready_state"] in ("interactive", "complete"), s
    assert s["error_page"] is False
    # The fields a login would need, found by name and id, with no values.
    got = {f["selector"]: f for f in s["fields"]}
    assert "#btn" in got and "#box" in got, s["fields"]
    assert got["#box"]["label"] == "Username"    # placeholder, not content
    assert all("value" not in f for f in s["fields"])
    assert any(f["type"] == "password" for f in s["fields"])


def test_smoke_a_real_error_page_is_detected(kiosk):
    """update.sh rolls a release back on this flag. Until now it had only ever
    been true in a fixture -- so this is the assertion that the rollback
    actually guards anything."""
    # Nothing listens on port 1. Chromium renders its own ERR_CONNECTION_REFUSED
    # page, which answers /v1/status perfectly happily.
    kiosk.post("/v1/navigate", json={"url": "http://127.0.0.1:1/"})
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        r = kiosk.get("/v1/inspect")
        # Status first, and the body in the message. Reading `error_page`
        # straight off .json() turned a 503 into `KeyError: 'error_page'`, which
        # names the symptom and hides the cause — and the cause was that inspect
        # could not run script on the error page and gave up rather than falling
        # back to the url.
        assert r.status_code == 200, f"inspect failed: {r.status_code} {r.text}"
        s = r.json()
        if s["error_page"]:
            break
        time.sleep(0.3)
    assert s["error_page"] is True, s
    # And the thing it is guarding against: everything else still looks fine.
    assert kiosk.get("/v1/status").json()["browser"] == "ok"


def test_smoke_inspect_answers_for_a_failed_page(kiosk):
    """Narrowly: it answers. Not what it says.

    Builds differ here in ways worth not asserting. Google Chrome leaves the url
    as the one you asked for, runs script on the error page, and finds
    `#main-frame-error`. Debian's Chromium on the Pi refused Runtime.evaluate
    outright and inspect raised — so /v1/inspect returned 503 on exactly the
    page it exists to describe, and `error_page` could never come back true.

    Whether the *detection* works is the test above, which is allowed to fail on
    a build where neither witness fires: that would be a finding, not a broken
    test. This one pins only that a page we cannot script is still a 200.
    """
    kiosk.post("/v1/navigate", json={"url": "http://127.0.0.1:1/"})
    time.sleep(1)
    r = kiosk.get("/v1/inspect")
    assert r.status_code == 200, f"inspect 503'd on a failed page: {r.text}"
    s = r.json()
    # "unknown" is the honest answer when script would not run, and it is how a
    # caller tells that apart from a page saying it is still loading.
    assert s["ready_state"] in ("complete", "loading", "interactive", "unknown"), s
    assert set(s) >= {"url", "title", "ready_state", "error_page", "has_media",
                      "scroll_y", "scroll_height", "fields"}, s


def test_smoke_the_wire_shape_update_sh_greps_for(kiosk, page_server):
    """The Pi has no jq, so update.sh matches `"error_page":false` in the raw
    body. tests/test_deploy.py pins that against a stub; this pins it against
    the real reply."""
    kiosk.post("/v1/navigate", json={"url": page_server})
    assert '"error_page":false' in kiosk.get("/v1/inspect").text


# --- input ------------------------------------------------------------------

def test_smoke_a_click_by_selector_reaches_the_page(kiosk, page_server):
    kiosk.post("/v1/navigate", json={"url": page_server})
    assert title_of(kiosk) == "ready"
    r = kiosk.post("/v1/input", json={"actions": [{"do": "click",
                                                   "selector": "#btn"}]})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert wait_title(kiosk, "clicked") == "clicked"


def test_smoke_a_click_by_coordinate_lands_where_it_was_aimed(kiosk, page_server):
    """The one the web UI depends on: it turns a click on the picture into
    coordinates, and if the mapping is off by any factor this hits nothing.

    #btn is at left:100 top:50, 200x100 — so (200, 100) is its middle, and
    (50, 20) is outside it. Both directions are checked, because a click that
    fires on *everything* would pass the first half on its own.
    """
    kiosk.post("/v1/navigate", json={"url": page_server})
    kiosk.post("/v1/input", json={"actions": [{"do": "click", "x": 50, "y": 20}]})
    assert wait_title(kiosk, "clicked", 1.0) == "ready", "a miss registered a hit"

    kiosk.post("/v1/input", json={"actions": [{"do": "click", "x": 200, "y": 100}]})
    assert wait_title(kiosk, "clicked") == "clicked"


def test_smoke_typing_reaches_the_focused_field(kiosk, page_server):
    """Click to focus, then type -- the shape of every login. The page reports
    what it received through its title, because inspect will not report a value
    and should not."""
    kiosk.post("/v1/navigate", json={"url": page_server})
    r = kiosk.post("/v1/input", json={"actions": [
        {"do": "click", "selector": "#box"},
        {"do": "type", "text": "kramav"}]})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    assert wait_title(kiosk, "typed:kramav") == "typed:kramav"


def test_smoke_a_key_press_submits_a_form(kiosk, page_server):
    kiosk.post("/v1/navigate", json={"url": page_server})
    r = kiosk.post("/v1/input", json={"actions": [
        {"do": "click", "selector": "#infield"},
        {"do": "type", "text": "hunter2"},
        {"do": "key", "key": "Enter"}]})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    assert wait_title(kiosk, "submitted") == "submitted"


def test_smoke_a_missing_element_stops_the_rest(kiosk, page_server):
    """The safety property, against a real browser: nothing is typed after a
    click that did not land."""
    kiosk.post("/v1/navigate", json={"url": page_server})
    r = kiosk.post("/v1/input", json={"actions": [
        {"do": "click", "selector": "#not-there"},
        {"do": "click", "selector": "#btn"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and len(body["results"]) == 1
    assert wait_title(kiosk, "clicked", 1.0) == "ready", "the second action ran"


def test_smoke_a_selector_reports_where_it_resolved(kiosk, page_server):
    """The coordinates come back so a caller can screenshot the same spot. #btn
    is absolutely positioned, so this is a known number, not a plausible one."""
    kiosk.post("/v1/navigate", json={"url": page_server})
    r = kiosk.post("/v1/input", json={"actions": [{"do": "click",
                                                   "selector": "#btn"}]})
    hit = r.json()["results"][0]
    assert (hit["x"], hit["y"]) == (200, 100), hit


# --- the whole point --------------------------------------------------------

def test_smoke_look_click_type_verify(kiosk, page_server):
    """The workflow this all exists for, end to end: an expired login on a box
    with no keyboard. See it, click it, type into it, confirm it took."""
    import base64
    kiosk.post("/v1/navigate", json={"url": page_server})

    before = kiosk.post("/v1/screenshot", json={}).json()
    assert png_size(base64.b64decode(before["image"]))[0] == before["width"]

    field = next(f for f in kiosk.get("/v1/inspect").json()["fields"]
                 if f["label"] == "Username")
    r = kiosk.post("/v1/input", json={"actions": [
        {"do": "click", "selector": field["selector"]},
        {"do": "type", "text": "kramav"}]})
    assert r.json()["ok"] is True, r.text

    assert wait_title(kiosk, "typed:kramav") == "typed:kramav"
    after = kiosk.post("/v1/screenshot", json={}).json()
    # The wall genuinely changed, and the picture is of the page after it.
    assert after["title"] == "typed:kramav"
    assert after["image"] != before["image"]
