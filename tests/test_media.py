"""Run: pytest.

Playback control with the CDP transport stubbed. Without a browser the JS in
_MEDIA_JS can't be executed, so what is checked here is everything around it:
that the action reaches Runtime.evaluate as a user gesture, that a page with no
video is a 404 and not a 500, and that *polling* state never wakes the room.
"""

import contextlib
import json

import pytest
from fastapi.testclient import TestClient

import roomctl
from agent import app as appmod
from agent import browser, display
from roomctl import cli

PAGES = [
    {"type": "page", "id": "T1", "url": "http://one/", "webSocketDebuggerUrl": "ws://one"},
    {"type": "page", "id": "T2", "url": "http://two/", "webSocketDebuggerUrl": "ws://two"},
]
STATE = {"playing": True, "muted": False, "volume": 80,
         "position": 12, "duration": 300}


def make_cfg(kind="chromium"):
    return {
        "token": "t", "home_url": "about:blank",
        "browser": {"kind": kind, "debug_port": 9222},
        "screens": [{"name": n, "position": "", "home_url": "about:blank"}
                    for n in ("left", "right")],
    }


class Calls(list):
    """The calls that went out. `.value` is what the page script returns —
    None means the page has no video or audio element."""
    value = STATE


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
            return {"result": {"value": calls.value}}
        yield call

    monkeypatch.setattr(browser, "_get", get)
    monkeypatch.setattr(browser, "_rpc", rpc)
    return calls


# --- browser.media ----------------------------------------------------------

def evaluate(cdp):
    return next(p for _, m, p in cdp if m == "Runtime.evaluate")


def test_action_and_value_reach_the_page(cdp):
    assert browser.media(make_cfg(), "left", "seek", -30) == STATE
    expr = evaluate(cdp)["expression"]
    assert json.dumps("seek") in expr and "-30.0" in expr


def test_play_claims_a_user_gesture(cdp):
    """Chromium refuses play() on a page nobody has clicked, and nobody can ever
    click a kiosk. Without this flag every play is silently ignored."""
    browser.media(make_cfg(), "left", "play")
    assert evaluate(cdp)["userGesture"] is True
    assert evaluate(cdp)["returnByValue"] is True


def test_named_screen_reaches_its_own_window(cdp):
    browser.media(make_cfg(), "right", "pause")
    assert [ws for ws, m, _ in cdp if m == "Runtime.evaluate"] == ["ws://two"]


def test_no_media_element_is_none_not_an_error(cdp):
    cdp.value = None
    assert browser.media(make_cfg(), "left", "state") is None


def test_bad_action_rejected_before_the_browser(cdp):
    with pytest.raises(ValueError, match="action must be"):
        browser.media(make_cfg(), "left", "eject")
    assert not cdp


def test_firefox_says_so_instead_of_crashing(cdp):
    with pytest.raises(NotImplementedError, match="chromium"):
        browser.media(make_cfg(kind="firefox"), "left", "play")


# --- HTTP -------------------------------------------------------------------

def write_cfg(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('token = "t"\nhome_url = "about:blank"\n'
                 '[browser]\nautolaunch = false\nkind = "chromium"\n'
                 '[[screen]]\nname = "left"\n[[screen]]\nname = "right"\n',
                 encoding="utf-8")
    return p


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOM_CONFIG", str(write_cfg(tmp_path)))
    with TestClient(appmod.app) as c:
        yield c


AUTH = {"Authorization": "Bearer t"}


def test_route_returns_the_state(client, cdp):
    r = client.post("/v1/media", json={"action": "toggle"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"ok": True, **STATE}


def test_nothing_playing_is_404(client, cdp):
    cdp.value = None
    r = client.post("/v1/media", json={"action": "pause"}, headers=AUTH)
    assert r.status_code == 404


def test_all_succeeds_if_any_screen_has_media(client, cdp, monkeypatch):
    """One video on a two-monitor wall is a success, not a failure."""
    seen = []

    def only_left(cfg, screen=None, action="state", value=0):
        seen.append(screen)
        return STATE if screen == "left" else None

    monkeypatch.setattr(browser, "media", only_left)
    r = client.post("/v1/media", json={"screen": "all", "action": "play"},
                    headers=AUTH)
    assert r.status_code == 200 and seen == ["left", "right"]


def test_auth_required(client):
    assert client.post("/v1/media", json={"action": "play"}).status_code == 401


def test_polling_state_does_not_wake_the_display(client, cdp, monkeypatch):
    """A controller left open on a desk polls this every 15s. If that counted as
    activity the room would never go dark again."""
    woke = []
    monkeypatch.setattr(display, "touch", lambda s, url=None: woke.append(s["name"]))
    client.post("/v1/media", json={"action": "state"}, headers=AUTH)
    assert woke == []
    client.post("/v1/media", json={"action": "play"}, headers=AUTH)
    assert woke == ["left"]


# --- roomctl ----------------------------------------------------------------

def test_cli_negative_seek(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(roomctl, "media", lambda *a, **k: seen.update(args=a) or {"ok": 1})
    # argparse only reads "-30" as a value because no option looks like a number.
    assert cli.main(["media", "seek", "-30"]) == 0
    assert seen["args"] == ("seek", None, None, -30)


def test_cli_media_defaults_to_state(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(roomctl, "media", lambda *a, **k: seen.update(args=a) or {"ok": 1})
    assert cli.main(["--screen", "right", "media"]) == 0
    assert seen["args"] == ("state", None, "right", 0)
