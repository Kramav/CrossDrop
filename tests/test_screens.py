"""Run: pytest.

Multi-screen and scroll, with the CDP transport stubbed. What matters here is
*routing* — that "right" reaches the right window and that a scroll turns into
the right protocol call — and none of that needs a browser to check.
"""

import contextlib

import pytest
from fastapi.testclient import TestClient

import roomctl
from agent import app as appmod
from agent import browser
from roomctl import cli

PAGES = [
    {"type": "page", "id": "T1", "url": "http://one/", "webSocketDebuggerUrl": "ws://one"},
    {"type": "page", "id": "T2", "url": "http://two/", "webSocketDebuggerUrl": "ws://two"},
]


def make_cfg(kind="chromium", names=("left", "right")):
    return {
        "token": "t", "home_url": "about:blank",
        "browser": {"kind": kind, "debug_port": 9222},
        "screens": [{"name": n, "position": "", "home_url": "about:blank"}
                    for n in names],
    }


@pytest.fixture
def cdp(monkeypatch):
    """Stub CDP. Yields the list of (ws_url, method, params) that went out."""
    calls = []
    browser._targets.clear()

    def get(port, path):
        return PAGES if path == "/json" else {"webSocketDebuggerUrl": "ws://browser"}

    @contextlib.contextmanager
    def rpc(ws_url):
        def call(method, params=None):
            calls.append((ws_url, method, params or {}))
            return {"targetId": "T9", "windowId": 7, "bounds": {}}
        yield call

    monkeypatch.setattr(browser, "_get", get)
    monkeypatch.setattr(browser, "_rpc", rpc)
    return calls


# --- routing ----------------------------------------------------------------

def test_named_screen_reaches_its_own_window(cdp):
    browser.navigate(make_cfg(), "https://x/", "right")
    assert [(ws, m) for ws, m, _ in cdp] == [("ws://two", "Page.navigate")]


def test_no_screen_means_the_first_one(cdp):
    """The compatibility guarantee: every pre-multi-monitor caller omits screen."""
    browser.navigate(make_cfg(), "https://x/", None)
    assert cdp[0][0] == "ws://one"


def test_target_id_is_remembered_then_reused(cdp):
    browser.navigate(make_cfg(), "https://x/", "right")
    assert browser._targets["right"] == "T2"
    browser.navigate(make_cfg(), "https://y/", "right")
    assert all(ws == "ws://two" for ws, _, _ in cdp)


def test_lost_window_falls_back_to_config_order(cdp):
    """A window closed and reopened gets a new target id. We must not wedge."""
    browser._targets["right"] = "GONE"
    browser.navigate(make_cfg(), "https://x/", "right")
    assert cdp[0][0] == "ws://two"
    assert browser._targets["right"] == "T2"


# --- scroll -----------------------------------------------------------------

def test_scroll_dy_is_a_wheel_event(cdp):
    browser.scroll(make_cfg(), "left", dy=450)
    ws, method, params = cdp[0]
    # A wheel event, not window.scrollBy: the PDF viewer ignores scripted scroll.
    assert (ws, method) == ("ws://one", "Input.dispatchMouseEvent")
    assert params["type"] == "mouseWheel" and params["deltaY"] == 450


def test_scroll_to_bottom_presses_end(cdp):
    browser.scroll(make_cfg(), "right", to="bottom")
    assert [m for _, m, _ in cdp] == ["Input.dispatchKeyEvent"] * 2   # down, up
    assert {p["windowsVirtualKeyCode"] for _, _, p in cdp} == {35}    # End


def test_scroll_to_top_presses_home(cdp):
    browser.scroll(make_cfg(), "left", to="top")
    assert {p["windowsVirtualKeyCode"] for _, _, p in cdp} == {36}


def test_bad_jump_target_rejected(cdp):
    with pytest.raises(ValueError):
        browser.scroll(make_cfg(), "left", to="sideways")


def test_firefox_says_so_instead_of_crashing(cdp):
    with pytest.raises(NotImplementedError, match="chromium"):
        browser.scroll(make_cfg(kind="firefox"), "left", dy=100)
    with pytest.raises(NotImplementedError, match="chromium"):
        browser.open_window(make_cfg(kind="firefox"), {"name": "right"})


# --- window placement -------------------------------------------------------

def test_second_window_is_moved_then_fullscreened(cdp):
    browser.open_window(make_cfg(), {"name": "right", "position": "1920,0",
                                     "home_url": "about:blank"})
    methods = [m for _, m, _ in cdp]
    assert methods == ["Target.createTarget", "Browser.getWindowForTarget",
                       "Browser.setWindowBounds", "Browser.setWindowBounds"]
    move, full = [p["bounds"] for _, m, p in cdp if m == "Browser.setWindowBounds"]
    # Order is the whole trick: Chromium refuses to move a fullscreen window.
    assert (move["left"], move["top"], move["windowState"]) == (1920, 0, "normal")
    assert full["windowState"] == "fullscreen"


def test_window_without_a_position_is_left_where_it_lands(cdp):
    browser.open_window(make_cfg(), {"name": "right", "position": "",
                                     "home_url": "about:blank"})
    assert [m for _, m, _ in cdp] == ["Target.createTarget"]


def test_first_window_is_moved_not_opened(cdp):
    """--kiosk already opened window 1 wherever the compositor wanted it. It has
    to be *moved*, or its `position` silently does nothing and picking its
    monitor becomes a matter of reordering the config until it guesses right."""
    browser.place(make_cfg(), {"name": "left", "position": "0,0",
                               "home_url": "about:blank"})
    methods = [m for _, m, _ in cdp]
    assert "Target.createTarget" not in methods          # no second window
    assert methods == ["Browser.getWindowForTarget",
                       "Browser.setWindowBounds", "Browser.setWindowBounds"]
    move, full = [p["bounds"] for _, m, p in cdp if m == "Browser.setWindowBounds"]
    # "normal" is also what un-fullscreens a --kiosk window so it can be moved.
    assert move["windowState"] == "normal" and move["left"] == 0
    assert full["windowState"] == "fullscreen"


# --- autoscroll -------------------------------------------------------------

def test_autoscroll_stops_when_the_screen_navigates(monkeypatch):
    """The bug most likely to ship unnoticed: a leftover loop scrolling whatever
    page lands next, with nothing in the UI to explain it."""
    cfg = make_cfg()
    appmod.app.state.cfg = cfg
    monkeypatch.setattr(browser, "scroll", lambda *a, **k: None)
    monkeypatch.setattr(browser, "navigate", lambda c, url, s=None: url)

    appmod._autoscroll_start(cfg, "left", 40)
    assert "left" in appmod._autoscroll
    appmod._go("https://elsewhere/", "left")
    assert "left" not in appmod._autoscroll


def test_autoscroll_on_one_screen_leaves_the_other_alone(monkeypatch):
    cfg = make_cfg()
    appmod.app.state.cfg = cfg
    monkeypatch.setattr(browser, "scroll", lambda *a, **k: None)
    monkeypatch.setattr(browser, "navigate", lambda c, url, s=None: url)

    appmod._autoscroll_start(cfg, "left", 40)
    appmod._autoscroll_start(cfg, "right", 40)
    appmod._go("https://x/", "left")
    assert "right" in appmod._autoscroll and "left" not in appmod._autoscroll
    appmod._autoscroll_stop("right")


# --- config + HTTP surface --------------------------------------------------

def write_cfg(tmp_path, extra=""):
    p = tmp_path / "config.toml"
    p.write_text('token = "t"\nhome_url = "about:blank"\n'
                 "[browser]\nautolaunch = false\nkind = \"chromium\"\n"
                 f"[upload]\nmax_mb = 1\n{extra}", encoding="utf-8")
    return p


def test_two_screens_from_config(tmp_path):
    cfg = appmod.load_config(write_cfg(tmp_path, """
[[screen]]
name = "left"
position = "0,0"
[[screen]]
name = "right"
position = "1920,0"
"""))
    assert [s["name"] for s in cfg["screens"]] == ["left", "right"]
    assert cfg["screens"][1]["position"] == "1920,0"
    assert cfg["screens"][0]["home_url"] == "about:blank"   # inherited


def test_home_url_carries_the_screen_name(tmp_path):
    """Both windows share a profile and a debug port, so the url is the only way
    the idle page can tell which monitor it is on. Without this every screen
    renders the first screen's name."""
    cfg = appmod.load_config(write_cfg(tmp_path, """
[[screen]]
name = "samsung"
[[screen]]
name = "acer"
"""))
    for s in cfg["screens"]:
        assert s["home_url"] == "about:blank"      # not a /home url, left alone

    cfg = appmod.load_config(write_cfg(tmp_path, """
[[screen]]
name = "samsung"
home_url = "http://100.1.2.3:8080/home"
[[screen]]
name = "acer"
home_url = "http://100.1.2.3:8080/home?x=1"
"""))
    assert cfg["screens"][0]["home_url"].endswith("/home?screen=samsung")
    assert cfg["screens"][1]["home_url"].endswith("?x=1&screen=acer")


def test_home_url_screen_name_not_doubled(tmp_path):
    cfg = appmod.load_config(write_cfg(tmp_path, """
[[screen]]
name = "acer"
home_url = "http://100.1.2.3:8080/home?screen=chosen"
"""))
    assert cfg["screens"][0]["home_url"].count("screen=") == 1


def test_unnamed_screens_get_names(tmp_path):
    cfg = appmod.load_config(write_cfg(tmp_path, "[[screen]]\n[[screen]]\n"))
    assert [s["name"] for s in cfg["screens"]] == ["main", "screen2"]


def test_duplicate_screen_names_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="duplicate"):
        appmod.load_config(write_cfg(
            tmp_path, '[[screen]]\nname = "a"\n[[screen]]\nname = "a"\n'))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOM_CONFIG", str(write_cfg(tmp_path, """
[[screen]]
name = "left"
[[screen]]
name = "right"
""")))
    with TestClient(appmod.app) as c:
        yield c


AUTH = {"Authorization": "Bearer t"}


def test_screens_route_lists_them(client):
    r = client.get("/v1/screens", headers=AUTH)
    assert r.status_code == 200
    assert [s["name"] for s in r.json()] == ["left", "right"]


def test_unknown_screen_is_404_not_500(client):
    r = client.post("/v1/scroll", json={"screen": "kitchen"}, headers=AUTH)
    assert r.status_code == 404
    assert "kitchen" in r.json()["detail"]


def test_home_status_hides_urls_and_needs_no_token(client):
    r = client.get("/home-status")
    assert r.status_code == 200
    body = r.json()
    assert [s["name"] for s in body["screens"]] == ["left", "right"]
    # Hostnames at most, never a full url — anyone on the tailnet can read this.
    assert all(s["showing"] is None or "/" not in s["showing"] for s in body["screens"])


def test_home_page_served_without_token(client):
    r = client.get("/home")
    assert r.status_code == 200
    assert "/home-status" in r.text


# --- roomctl ----------------------------------------------------------------

@pytest.fixture
def spy(monkeypatch):
    seen = {}

    def fake(target=None, screen=None, **kw):
        seen.update(target=target, screen=screen, **kw)
        return {"ok": True}

    monkeypatch.setattr(roomctl, "scroll", fake)
    return seen


def test_cli_scroll_down_is_positive(spy, capsys):
    assert cli.main(["scroll", "--down"]) == 0
    assert spy["dy"] > 0


def test_cli_scroll_up_is_negative(spy, capsys):
    assert cli.main(["scroll", "--up"]) == 0
    assert spy["dy"] < 0


def test_cli_scroll_bottom_and_screen(spy, capsys):
    assert cli.main(["--screen", "right", "scroll", "--bottom"]) == 0
    assert spy["to"] == "bottom" and spy["screen"] == "right"
