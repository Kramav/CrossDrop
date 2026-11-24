"""Streaming from a PC: the agent's half (/v1/stream and the kiosk routes).

The browser is stubbed. The kiosk side is played by calling the kiosk routes
in-process from the navigate stub, which is exactly when the real kiosk page
would call them: right after the agent points it at /stream-view.
"""

import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from agent import app as appmod
from agent import browser, display

TOKEN = "test-token"
H = {"Authorization": f"Bearer {TOKEN}"}
OFFER = "v=0 offer"
ANSWER = "v=0 answer"


@pytest.fixture
def wall(tmp_path, monkeypatch):
    """An agent with a chromium config and a fake wall: `shown` is each screen's
    url, `navs` every navigate in order. `kiosk` decides what the kiosk page
    does when it lands on a stream url -- answer, by default."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'token = "{TOKEN}"\nhome_url = "about:blank"\n'
                   '[browser]\nkind = "chromium"\nautolaunch = false\n',
                   encoding="utf-8")
    monkeypatch.setenv("CROSSDROP_CONFIG", str(cfg))
    state = {"shown": {}, "navs": [], "kiosk": "answer"}

    def navigate_one(s, url):
        state["navs"].append(url)
        state["shown"][s["name"]] = url
        if appmod._streaming(url) and state["kiosk"] == "answer":
            sid = parse_qs(urlparse(url).query)["id"][0]
            assert appmod.stream_offer(sid)["sdp"] == OFFER
            appmod.stream_answer(appmod.AnswerIn(id=sid, sdp=ANSWER))
        return url

    monkeypatch.setattr(appmod, "_navigate_one", navigate_one)
    monkeypatch.setattr(browser, "current_url",
                        lambda cfg, name=None: state["shown"].get(name, "https://before.example/"))
    appmod._stream.clear()
    with TestClient(appmod.app) as c:
        state["c"] = c
        yield state
    appmod._stream.clear()


def start(w, screen=None):
    return w["c"].post("/v1/stream", headers=H, json={"sdp": OFFER, "screen": screen})


def sid_of(url):
    return parse_qs(urlparse(url).query)["id"][0]


def test_offer_in_answer_out(wall):
    r = start(wall)
    assert r.status_code == 200, r.text
    assert r.json()["sdp"] == ANSWER
    url = wall["navs"][-1]
    assert urlparse(url).path == "/stream-view", url
    # Single use: the url sits in /v1/status, and must not replay the offer.
    assert wall["c"].get(f"/stream-offer?id={sid_of(url)}").status_code == 404


def test_the_kiosk_learns_where_to_go_back_to(wall, monkeypatch):
    seen = {}
    real = appmod.stream_offer
    monkeypatch.setattr(appmod, "stream_offer", lambda sid: seen.setdefault("o", real(sid)))
    assert start(wall).status_code == 200
    assert seen["o"]["back"] == "https://before.example/"


def test_a_stream_plays_on_one_screen(wall):
    r = start(wall, "all")
    assert r.status_code == 422 and "one" in r.text


def test_a_token_is_required(wall):
    r = wall["c"].post("/v1/stream", json={"sdp": OFFER})
    assert r.status_code in (401, 403)
    assert not wall["navs"]


def test_firefox_says_so_rather_than_hanging(wall, monkeypatch):
    """A Firefox kiosk hides its address from WebRTC and the PC would wait out
    the whole timeout to learn nothing. 501, before the wall is touched."""
    monkeypatch.setitem(appmod.app.state.cfg["browser"], "kind", "firefox")
    assert start(wall).status_code == 501
    assert not wall["navs"]


def test_a_silent_kiosk_is_a_504(wall, monkeypatch):
    monkeypatch.setattr(appmod, "STREAM_WAIT", 0.05)
    wall["kiosk"] = "silent"
    assert start(wall).status_code == 504


def test_the_kiosk_routes_need_the_id(wall):
    assert start(wall).status_code == 200
    c = wall["c"]
    assert c.get("/stream-offer?id=guess").status_code == 404
    assert c.post("/stream-answer", json={"id": "guess", "sdp": "x"}).status_code == 404
    assert c.get("/stream-alive?id=guess").status_code == 404


def test_the_heartbeat_keeps_the_display_awake(wall, monkeypatch):
    touched = []
    monkeypatch.setattr(display, "touch", lambda s, url=None: touched.append(s["name"]))
    assert start(wall).status_code == 200
    sid = sid_of(wall["navs"][-1])
    assert wall["c"].get(f"/stream-alive?id={sid}").status_code == 200
    assert touched == [appmod.app.state.cfg["screens"][0]["name"]]


def test_stop_puts_back_what_was_there(wall):
    assert start(wall).status_code == 200
    r = wall["c"].delete("/v1/stream", headers=H)
    assert r.json() == {"ok": True, "stopped": True}
    assert wall["navs"][-1] == "https://before.example/"


def test_stop_never_undoes_a_later_navigate(wall):
    assert start(wall).status_code == 200
    name = appmod.app.state.cfg["screens"][0]["name"]
    wall["shown"][name] = "https://chosen-later.example/"
    n = len(wall["navs"])
    assert wall["c"].delete("/v1/stream", headers=H).json()["stopped"] is True
    assert len(wall["navs"]) == n, "stop navigated away from a page shown after it"


def test_replacing_a_stream_goes_back_past_both(wall):
    """Stream, stream again, stop: the wall returns to the page from before the
    first one, not to the first stream's dead page."""
    assert start(wall).status_code == 200
    assert start(wall).status_code == 200
    wall["c"].delete("/v1/stream", headers=H)
    assert wall["navs"][-1] == "https://before.example/"


def test_a_stream_is_never_restored_after_a_restart(wall):
    assert not appmod._still_there(appmod.app.state.cfg,
                                   "http://100.64.0.1:8080/stream-view?id=x")


def test_https_url_comes_from_serve_config(monkeypatch):
    class Done:
        stdout = json.dumps({"Web": {"wall.tail1234.ts.net:443": {}}})

    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **kw: Done())
    monkeypatch.setattr(appmod, "_https", {"url": "", "checked": 0.0})
    assert appmod.https_url() == "https://wall.tail1234.ts.net/"


def test_no_serve_config_means_no_https_url(monkeypatch):
    class Done:
        stdout = "{}"

    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **kw: Done())
    monkeypatch.setattr(appmod, "_https", {"url": "", "checked": 0.0})
    assert appmod.https_url() == ""


def test_one_disable_features_flag(tmp_path, monkeypatch):
    """Chromium honours only the last --disable-features. Two would quietly
    switch the mDNS hiding back on, and every stream would time out."""
    seen = []
    monkeypatch.setattr(browser.subprocess, "Popen", lambda argv, **kw: seen.append(argv))
    monkeypatch.setattr(browser, "wait_ready", lambda *a, **kw: None)
    monkeypatch.setattr(browser, "_exe", lambda kind, path="": "/usr/bin/chromium")
    browser.launch({"home_url": "about:blank",
                    "browser": {"kind": "chromium", "path": "", "debug_port": 9222,
                                "disk_cache_mb": 100,
                                "profile_dir": str(tmp_path / "profile")}})
    off = [a for a in seen[0] if a.startswith("--disable-features=")]
    assert len(off) == 1 and "WebRtcHideLocalIpsWithMdns" in off[0], seen[0]
