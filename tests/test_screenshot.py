"""Run: pytest.

Screenshot capture with the CDP transport stubbed, the same way test_media.py
does it. Without a browser nothing here can prove a picture *looks* right --
what it proves is the part that would silently ruin one: that the clip is
pinned to CSS pixels, that a region is clamped rather than sent through as
nonsense, and that photographing a screen never wakes the room.
"""

import base64
import contextlib
import json

import pytest
from fastapi.testclient import TestClient

import roomctl
from agent import app as appmod
from agent import browser
from agent.app import app
from roomctl import cli

TOKEN = "test-token"
H = {"Authorization": f"Bearer {TOKEN}"}

# 1x1 png, so the bytes that come back are a real image rather than a marker.
PIXEL = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001" "0d0a2db4"
    "0000000049454e44ae426082")).decode()

PAGES = [
    {"type": "page", "id": "T1", "url": "http://one/", "title": "One",
     "webSocketDebuggerUrl": "ws://one"},
    {"type": "page", "id": "T2", "url": "http://two/", "title": "Two",
     "webSocketDebuggerUrl": "ws://two"},
]


def make_cfg(kind="chromium"):
    return {"token": "t", "home_url": "about:blank",
            "browser": {"kind": kind, "debug_port": 9222},
            "screens": [{"name": n, "position": "", "home_url": "about:blank"}
                        for n in ("left", "right")]}


class Calls(list):
    """Calls that went out. `viewport` is what getLayoutMetrics reports."""
    viewport = {"clientWidth": 1920, "clientHeight": 1080}


@pytest.fixture
def cdp(monkeypatch):
    calls = Calls()
    browser._targets.clear()

    def get(port, path):
        return PAGES if path == "/json" else {"webSocketDebuggerUrl": "ws://browser"}

    @contextlib.contextmanager
    def rpc(ws_url):
        def call(method, params=None):
            calls.append((ws_url, method, params or {}))
            if method == "Page.getLayoutMetrics":
                return {"cssLayoutViewport": calls.viewport}
            return {"data": PIXEL}
        yield call

    monkeypatch.setattr(browser, "_get", get)
    monkeypatch.setattr(browser, "_rpc", rpc)
    return calls


def shot_params(cdp):
    return next(p for _, m, p in cdp if m == "Page.captureScreenshot")


# --- browser.screenshot -----------------------------------------------------

def test_it_captures_the_screen_you_named(cdp):
    r = browser.screenshot(make_cfg(), "right")
    assert r["image"] == PIXEL
    assert r["url"] == "http://two/" and r["title"] == "Two"
    assert ("ws://two", "Page.captureScreenshot") == next(
        (w, m) for w, m, _ in cdp if m == "Page.captureScreenshot")


def test_the_clip_is_pinned_to_css_pixels(cdp):
    """The whole coordinate contract. Without an explicit clip Chromium captures
    at the device pixel ratio, so a 1920-wide viewport comes back 3840 wide on a
    HiDPI panel and anything mapping the image back onto the page is off by 2x.
    """
    browser.screenshot(make_cfg(), "left")
    clip = shot_params(cdp)["clip"]
    assert clip == {"x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1}


def test_reported_size_is_the_size_you_get(cdp):
    r = browser.screenshot(make_cfg(), "left")
    assert (r["width"], r["height"]) == (1920, 1080)


def test_a_region_is_clipped_and_reported(cdp):
    r = browser.screenshot(make_cfg(), "left",
                           region={"x": 100, "y": 50, "width": 400, "height": 300})
    assert shot_params(cdp)["clip"] == {"x": 100, "y": 50, "width": 400,
                                        "height": 300, "scale": 1}
    assert (r["width"], r["height"]) == (400, 300)


def test_an_oversized_region_is_clamped_not_refused(cdp):
    """An off-by-a-bit rect is worth a slightly smaller picture, not a 422 --
    and because the clamp is invisible otherwise, the reply reports the size
    that came back rather than the one that was asked for."""
    r = browser.screenshot(make_cfg(), "left",
                           region={"x": 1800, "y": 1000, "width": 9999, "height": 9999})
    assert (r["width"], r["height"]) == (120, 80)
    assert shot_params(cdp)["clip"]["width"] == 120


def test_a_zero_dimension_means_to_the_edge(cdp):
    """It is also the model default, so `{"x": 100, "y": 100}` is "everything
    below and right of here" rather than a rejected empty rect."""
    r = browser.screenshot(make_cfg(), "left", region={"x": 100, "y": 80})
    assert (r["width"], r["height"]) == (1820, 1000)


def test_a_region_with_no_area_is_an_error(cdp):
    with pytest.raises(ValueError, match="empty"):
        browser.screenshot(make_cfg(), "left",
                           region={"x": 0, "y": 0, "width": -5, "height": 100})


def test_quality_rides_along_only_where_it_means_something(cdp):
    browser.screenshot(make_cfg(), "left", format="jpeg", quality=40)
    assert shot_params(cdp)["quality"] == 40
    cdp.clear()
    # png ignores quality and some builds complain at being sent it.
    browser.screenshot(make_cfg(), "left", format="png")
    assert "quality" not in shot_params(cdp)


def test_a_bad_format_or_quality_is_rejected_before_the_browser(cdp):
    for kwargs in ({"format": "bmp"}, {"quality": 0}, {"quality": 101}):
        with pytest.raises(ValueError):
            browser.screenshot(make_cfg(), "left", **kwargs)
    assert not cdp                      # nothing was asked of the browser


def test_a_viewport_we_cannot_read_still_produces_a_picture(cdp):
    """getLayoutMetrics is missing on odd targets and old builds. A guessed clip
    beats an exception: a screenshot exists to answer "what is up there", and
    the wrong size still answers it."""
    cdp.viewport = {}
    r = browser.screenshot(make_cfg(), "left")
    assert (r["width"], r["height"]) == (800, 600)


def test_firefox_says_so_rather_than_capturing_the_wrong_window():
    with pytest.raises(NotImplementedError, match="CDP"):
        browser.screenshot(make_cfg("firefox"), "left")


# --- the route --------------------------------------------------------------

def write_config(tmp_path, kind="chromium"):
    p = tmp_path / "config.toml"
    p.write_text(f'token = "{TOKEN}"\nhome_url = "about:blank"\n'
                 f'[[screen]]\nname = "left"\n[[screen]]\nname = "right"\n'
                 f'[browser]\nkind = "{kind}"\nautolaunch = false\n', encoding="utf-8")
    return p


@pytest.fixture
def client(tmp_path, monkeypatch, cdp):
    monkeypatch.setenv("ROOM_CONFIG", str(write_config(tmp_path)))
    with TestClient(app) as c:
        yield c


def test_the_route_returns_an_image_and_what_the_page_says_it_is(client):
    r = client.post("/v1/screenshot", headers=H, json={"screen": "right"})
    assert r.status_code == 200
    body = r.json()
    assert base64.b64decode(body["image"])          # real bytes, not a marker
    assert body["screen"] == "right" and body["url"] == "http://two/"
    assert body["title"] == "Two" and body["format"] == "png"
    assert body["taken_at"] > 0 and body["took_ms"] >= 0


def test_it_needs_the_token(client):
    assert client.post("/v1/screenshot", json={}).status_code == 401


def test_an_unknown_screen_is_a_404_like_everywhere_else(client):
    r = client.post("/v1/screenshot", headers=H, json={"screen": "nope"})
    assert r.status_code == 404


def test_a_bad_format_is_a_422(client):
    r = client.post("/v1/screenshot", headers=H, json={"format": "bmp"})
    assert r.status_code == 422


def test_a_dead_browser_is_a_503(client, monkeypatch):
    def gone(*a, **k):
        raise OSError("window gone")
    monkeypatch.setattr(appmod.browser, "screenshot", gone)
    assert client.post("/v1/screenshot", headers=H, json={}).status_code == 503


def test_looking_at_a_screen_does_not_wake_the_room(client, monkeypatch):
    """A controller polling this would otherwise keep the display lit all night,
    which is the exact thing the idle timer exists to prevent. Same rule as
    /v1/window and media action=state."""
    woken = []
    monkeypatch.setattr(appmod.display, "touch",
                        lambda s, url=None: woken.append(s["name"]))
    client.post("/v1/screenshot", headers=H, json={})
    assert woken == []


def test_status_advertises_it(client):
    assert "screenshot" in client.get("/v1/status", headers=H).json()["supports"]


@pytest.mark.parametrize("cdp_kind", ["firefox"])
def test_firefox_501s_through_the_route(tmp_path, monkeypatch, cdp, cdp_kind):
    monkeypatch.setenv("ROOM_CONFIG", str(write_config(tmp_path, kind=cdp_kind)))
    with TestClient(app) as c:
        r = c.post("/v1/screenshot", headers=H, json={})
        assert r.status_code == 501
        assert "screenshot" not in c.get("/v1/status", headers=H).json()["supports"]


# --- the client and the CLI -------------------------------------------------

def test_the_client_decodes_to_real_bytes(client, monkeypatch):
    monkeypatch.setattr(roomctl.Client, "_call",
                        lambda self, m, p, **kw: client.post(p, headers=H,
                                                             **kw).json())
    with roomctl.Client("http://x", TOKEN) as c:
        shot = c.screenshot(screen="left")
    assert base64.b64decode(shot["image"])


def test_the_cli_writes_the_file_and_keeps_stdout_pipeable(client, monkeypatch,
                                                           tmp_path, capsys,
                                                           cli_target):
    """Every other command prints the agent's reply verbatim so it pipes into
    jq. A megabyte of base64 would make that useless, so the image goes to the
    file and only what is worth reading goes to stdout."""
    monkeypatch.setattr(roomctl.Client, "screenshot",
                        lambda self, *a, **k: client.post("/v1/screenshot", headers=H,
                                                          json={}).json())
    out = tmp_path / "wall.png"
    assert cli.main(["shot", "-o", str(out)]) == 0
    assert out.read_bytes() == base64.b64decode(PIXEL)
    printed = json.loads(capsys.readouterr().out)
    assert "image" not in printed                   # not in the terminal
    assert printed["written"] == str(out) and printed["bytes"] > 0


def test_the_cli_rejects_a_malformed_region(capsys):
    assert cli.main(["shot", "--region", "0,0,800"]) == 1
    assert "x,y,width,height" in capsys.readouterr().err
