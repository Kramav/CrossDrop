"""Streaming from a PC: the agent's half (/v1/stream and the kiosk routes).

The browser is stubbed. The kiosk side is played by calling the kiosk routes
in-process from the navigate stub, which is exactly when the real kiosk page
would call them: right after the agent points it at /stream-view.
"""

import json
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from agent import app as appmod
from agent import browser, display

TOKEN = "test-token"
H = {"Authorization": f"Bearer {TOKEN}"}
OFFER = "v=0 offer"
# What a kiosk Chromium with its addresses hidden answers: host candidates
# under random .local names, CRLF line endings, as on the wire.
ANSWER = ("v=0\r\n"
          "a=candidate:1467250027 1 udp 2122260223 0b1c1f3e-aaaa.local 54400 typ host"
          " generation 0\r\n"
          "a=candidate:2 1 udp 2122194687 7d0e2c11-bbbb.local 54401 typ host generation 0\r\n"
          "a=end-of-candidates\r\n")


@pytest.fixture
def wall(tmp_path, monkeypatch):
    """An agent with a chromium config and a fake wall: `shown` is each screen's
    url, `navs` every navigate in order. `kiosk` decides what the kiosk page
    does when it lands on a stream url -- answer, by default."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'token = "{TOKEN}"\nhome_url = "about:blank"\n'
                   '[browser]\nkind = "chromium"\nautolaunch = false\n'
                   # Two screens, as the two halves of a split panel would be.
                   '[[screen]]\nname = "left"\n[[screen]]\nname = "right"\n',
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
    # This machine's own route must not leak into answers; tests that want a
    # twin set an address themselves.
    monkeypatch.setattr(appmod, "_box_addresses", lambda: [])
    monkeypatch.setattr(browser, "current_url",
                        lambda cfg, name=None: state["shown"].get(name, "https://before.example/"))
    appmod._streams.clear()
    with TestClient(appmod.app) as c:
        state["c"] = c
        yield state
    appmod._streams.clear()


def start(w, screen="left"):
    return w["c"].post("/v1/stream", headers=H, json={"sdp": OFFER, "screen": screen})


def sid_of(url):
    return parse_qs(urlparse(url).query)["id"][0]


def stop(w, **params):
    return w["c"].delete("/v1/stream", headers=H, params=params)


# --- the round trip -----------------------------------------------------------

def test_offer_in_answer_out(wall):
    r = start(wall)
    assert r.status_code == 200, r.text
    assert r.json()["sdp"] == ANSWER        # no address to twin with here
    assert r.json()["handle"]
    url = wall["navs"][-1]
    assert urlparse(url).path == "/stream-view", url


def test_the_kiosk_is_sent_to_the_socket_not_the_host_header(wall):
    """From the https UI the Host header is a .ts.net name, which a Pi with
    Tailscale DNS off cannot resolve. The kiosk gets the address the request
    arrived on instead -- which it, on the same box, can always reach."""
    wall["c"].post("/v1/stream", json={"sdp": OFFER, "screen": "left"},
                   headers={**H, "Host": "wall.tail1234.ts.net"})
    assert "ts.net" not in wall["navs"][-1], wall["navs"][-1]


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


# --- the tailnet address goes to our sender only ----------------------------

def test_hidden_candidates_get_a_twin_with_the_boxs_address():
    out = appmod._with_addresses(ANSWER, ["100.64.0.1"])
    lines = out.split("\r\n")
    assert ("a=candidate:14672500271 1 udp 2122260223 100.64.0.1 54400 typ host"
            " generation 0") in lines
    assert "a=candidate:21 1 udp 2122194687 100.64.0.1 54401 typ host generation 0" in lines
    # The originals stay, every line keeps its CRLF, and nothing else moves.
    assert ANSWER.split("\r\n")[1] in lines
    assert "\n" not in out.replace("\r\n", "")
    assert lines[-2] == "a=end-of-candidates"


def test_real_addresses_and_no_address_are_left_alone():
    raw = "a=candidate:1 1 udp 1 192.168.1.5 5000 typ host\r\n"
    assert appmod._with_addresses(raw, ["100.64.0.1"]) == raw
    assert appmod._with_addresses(ANSWER, []) == ANSWER


def test_the_pc_gets_the_twin(wall, monkeypatch):
    monkeypatch.setattr(appmod, "_box_addresses", lambda: ["192.168.1.20"])
    sdp = start(wall).json()["sdp"]
    assert " 192.168.1.20 54400 typ host" in sdp


def test_the_twin_is_the_default_route_address():
    """Kiosk Chromium listens on the default-route interface only (measured),
    so that -- never loopback, never a guess -- is the address the twin gets."""
    got = appmod._box_addresses()
    assert len(got) <= 1
    if got:
        import ipaddress
        ip = ipaddress.ip_address(got[0])
        assert not ip.is_loopback and not ip.is_unspecified


def test_kiosk_chromium_keeps_hiding_its_addresses(tmp_path, monkeypatch):
    """The twin above replaces switching mDNS hiding off, which handed every
    site the kiosk renders this box's LAN and tailnet addresses."""
    seen = []
    monkeypatch.setattr(browser.subprocess, "Popen", lambda argv, **kw: seen.append(argv))
    monkeypatch.setattr(browser, "wait_ready", lambda *a, **kw: None)
    monkeypatch.setattr(browser, "_exe", lambda kind, path="": "/usr/bin/chromium")
    browser.launch({"home_url": "about:blank",
                    "browser": {"kind": "chromium", "path": "", "debug_port": 9222,
                                "disk_cache_mb": 100,
                                "profile_dir": str(tmp_path / "profile")}})
    assert not [a for a in seen[0] if "WebRtcHideLocalIpsWithMdns" in a], seen[0]
    assert len([a for a in seen[0] if a.startswith("--disable-features=")]) <= 1


# --- the kiosk routes ---------------------------------------------------------

def test_the_kiosk_routes_need_the_id(wall):
    assert start(wall).status_code == 200
    c = wall["c"]
    assert c.get("/stream-offer?id=guess").status_code == 404
    assert c.post("/stream-answer", json={"id": "guess", "sdp": "x"}).status_code == 404
    assert c.get("/stream-alive?id=guess").status_code == 404


def test_a_reloaded_stream_page_goes_back_where_it_came_from(wall):
    """Reload, or a renderer crash, fetches the offer a second time. The stream
    cannot be renegotiated without the PC, so it is over -- and the page is told
    where the screen was before, rather than guessing /home."""
    assert start(wall).status_code == 200
    r = wall["c"].get(f"/stream-offer?id={sid_of(wall['navs'][-1])}")
    assert r.status_code == 410
    assert r.json()["detail"]["back"] == "https://before.example/"
    assert "left" not in appmod._streams


def test_the_heartbeat_holds_the_idle_timer_without_waking(wall, monkeypatch):
    touched, powered = [], []
    monkeypatch.setattr(display, "_last", {})
    monkeypatch.setattr(display, "power", lambda on: powered.append(on))
    assert start(wall).status_code == 200
    sid = sid_of(wall["navs"][-1])
    assert wall["c"].get(f"/stream-alive?id={sid}").status_code == 200
    touched = list(display._last)
    assert touched == ["left"]
    # Someone switched the monitors off: a stream left running must not keep
    # switching them back on.
    assert powered == [], powered


# --- one stream per screen ----------------------------------------------------

def test_two_screens_stream_at_once(wall):
    """A split panel mixing streams: one per part, independently."""
    assert start(wall, "left").status_code == 200
    assert start(wall, "right").status_code == 200
    assert set(appmod._streams) == {"left", "right"}
    assert all(appmod._streaming(wall["shown"][n]) for n in ("left", "right"))


def test_restreaming_one_screen_leaves_the_other_alone(wall):
    assert start(wall, "left").status_code == 200
    assert start(wall, "right").status_code == 200
    right = appmod._streams["right"]["id"]
    assert start(wall, "left").status_code == 200
    assert appmod._streams["right"]["id"] == right


def test_a_page_beside_a_stream_is_untouched(wall):
    """Mixing: navigating the other half while one streams changes nothing about
    the stream, and stopping the stream changes nothing about the page."""
    assert start(wall, "left").status_code == 200
    wall["c"].post("/v1/navigate", headers=H,
                   json={"url": "https://page.example/", "screen": "right"})
    assert "left" in appmod._streams
    stop(wall, screen="left")
    assert wall["shown"]["right"] == "https://page.example/"


# --- stopping -----------------------------------------------------------------

def test_stop_puts_back_what_was_there(wall):
    h = start(wall).json()["handle"]
    r = stop(wall, screen="left", handle=h)
    assert r.json() == {"ok": True, "stopped": ["left"]}
    assert wall["navs"][-1] == "https://before.example/"


def test_stop_never_undoes_a_later_navigate(wall):
    assert start(wall).status_code == 200
    wall["shown"]["left"] = "https://chosen-later.example/"
    n = len(wall["navs"])
    assert stop(wall, screen="left").json()["stopped"] == ["left"]
    assert len(wall["navs"]) == n, "stop navigated away from a page shown after it"


def test_replacing_a_stream_goes_back_past_both(wall):
    """Stream, stream again, stop: the wall returns to the page from before the
    first one, not to the first stream's dead page."""
    assert start(wall).status_code == 200
    assert start(wall).status_code == 200
    stop(wall, screen="left")
    assert wall["navs"][-1] == "https://before.example/"


def test_the_first_pcs_stop_cannot_end_the_second_pcs_stream(wall):
    """PC2 takes the screen over; PC1's Stop (or its tab closing) must end
    PC1's stream -- which is already gone -- and leave PC2's playing."""
    h1 = start(wall).json()["handle"]
    h2 = start(wall).json()["handle"]
    r = stop(wall, screen="left", handle=h1)
    assert r.json()["stopped"] == []
    assert appmod._streams["left"]["handle"] == h2
    assert appmod._streaming(wall["shown"]["left"])


def test_stopping_one_screen_keeps_the_other_streaming(wall):
    assert start(wall, "left").status_code == 200
    assert start(wall, "right").status_code == 200
    assert stop(wall, screen="left").json()["stopped"] == ["left"]
    assert wall["shown"]["left"] == "https://before.example/"
    assert appmod._streaming(wall["shown"]["right"])
    assert set(appmod._streams) == {"right"}


def test_stop_without_a_screen_ends_every_stream(wall):
    assert start(wall, "left").status_code == 200
    assert start(wall, "right").status_code == 200
    assert sorted(stop(wall).json()["stopped"]) == ["left", "right"]
    assert not appmod._streams


def test_stop_names_an_unknown_screen(wall):
    assert stop(wall, screen="nope").status_code == 404


def test_stop_all_survives_a_screen_that_vanished(wall):
    """One stream on a screen that no longer exists must not stop the others
    being put back."""
    assert start(wall, "left").status_code == 200
    assert start(wall, "right").status_code == 200
    appmod._streams["gone"] = appmod._streams.pop("left")
    appmod._streams["gone"]["screen"] = "gone"
    r = stop(wall)
    assert r.json()["ok"] is False and "gone" in r.json()["errors"][0]
    assert wall["shown"]["right"] == "https://before.example/"


# --- renames, relaunches and timeouts -----------------------------------------

def test_a_rename_carries_the_stream_with_it():
    """Keyed by name, a rename stranded the stream: the heartbeat 404'd (the
    display slept mid-film) and Stop could not find it."""
    appmod._streams.clear()
    appmod._streams["left"] = {"screen": "left", "answered": threading.Event()}
    old = [{"name": "left", "output": "HDMI-1-L"}, {"name": "right", "output": "HDMI-1-R"}]
    new = [{"name": "west", "output": "HDMI-1-L"}, {"name": "right", "output": "HDMI-1-R"}]
    appmod._rekey_streams(old, new)
    assert list(appmod._streams) == ["west"]
    assert appmod._streams["west"]["screen"] == "west"
    appmod._streams.clear()


def test_a_resplit_ends_a_stream_whose_screen_is_gone():
    appmod._streams.clear()
    st = {"screen": "HDMI-1-L", "answered": threading.Event(), "ended": ""}
    appmod._streams["HDMI-1-L"] = st
    appmod._rekey_streams([{"name": "HDMI-1-L", "output": "HDMI-1-L"}],
                          [{"name": "HDMI-1", "output": "HDMI-1"}])
    assert not appmod._streams and st["ended"] and st["answered"].is_set()


def test_a_timeout_leaves_nothing_behind(wall, monkeypatch):
    """The PC gave up: no entry left for a late answer to land in, and the
    screen is not left on a page waiting for nobody."""
    monkeypatch.setattr(appmod, "STREAM_WAIT", 0.05)
    wall["kiosk"] = "silent"
    assert start(wall).status_code == 504
    assert not appmod._streams
    assert wall["shown"]["left"] == "https://before.example/"


def test_a_stop_during_a_start_answers_at_once(wall, monkeypatch):
    """A start still waiting on the kiosk wakes when it is stopped, and says
    so -- not a 504 "did not answer" fifteen seconds later."""
    monkeypatch.setattr(appmod, "STREAM_WAIT", 10)
    wall["kiosk"] = "silent"
    got = {}
    t = threading.Thread(target=lambda: got.setdefault("r", start(wall)))
    began = time.monotonic()
    t.start()
    while "left" not in appmod._streams:
        time.sleep(0.01)
    appmod.stream_stop(screen="left")
    t.join(5)
    assert got["r"].status_code == 409 and "stopped" in got["r"].text
    assert time.monotonic() - began < 5


def test_a_takeover_tells_the_first_pc_it_was_replaced(wall, monkeypatch):
    monkeypatch.setattr(appmod, "STREAM_WAIT", 10)
    wall["kiosk"] = "silent"
    got = {}
    t = threading.Thread(target=lambda: got.setdefault("r", start(wall)))
    t.start()
    while "left" not in appmod._streams:
        time.sleep(0.01)
    wall["kiosk"] = "answer"
    assert start(wall).status_code == 200
    t.join(5)
    assert got["r"].status_code == 409 and "took that screen over" in got["r"].text


def test_a_relaunch_ends_every_stream(wall):
    assert start(wall, "left").status_code == 200
    appmod._end_all_streams("the display's browser restarted")
    assert not appmod._streams


def test_a_stream_is_never_restored_after_a_restart(wall):
    assert not appmod._still_there(appmod.app.state.cfg,
                                   "http://100.64.0.1:8080/stream-view?id=x")


def test_status_reports_whose_stream_is_where(wall):
    """How a sender finds its stream again after a rename moves it."""
    h = start(wall).json()["handle"]
    screens = {s["name"]: s["stream"]
               for s in wall["c"].get("/v1/status", headers=H).json()["screens"]}
    assert screens == {"left": h, "right": ""}


# --- https_url ----------------------------------------------------------------

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


# --- binding (agent/__main__.py) ----------------------------------------------

def test_the_agent_can_rebind_straight_after_a_restart():
    """The kiosk polls the agent constantly, so a restart always leaves
    connections the agent closed in TIME_WAIT. Without SO_REUSEADDR the new
    process cannot bind for ~60s and update.sh rolls a good release back."""
    import os
    import socket

    from agent.__main__ import listeners

    if os.name == "nt":
        pytest.skip("SO_REUSEADDR means port stealing on Windows; not set there")
    (srv,) = listeners("127.0.0.1", 0)
    port = srv.getsockname()[1]
    srv.listen()
    cli = socket.create_connection(("127.0.0.1", port))
    conn, _ = srv.accept()
    conn.close()                    # the server closes first: TIME_WAIT is ours
    cli.close()
    srv.close()
    (again,) = listeners("127.0.0.1", port)
    again.close()


def test_loopback_is_added_beside_a_real_address_only():
    from agent.__main__ import listeners

    for host in ("127.0.0.1", "0.0.0.0"):
        socks = listeners(host, 0)
        assert len(socks) == 1, host
        for s in socks:
            s.close()


def test_an_empty_crossdrop_host_refuses_to_start(monkeypatch):
    """`tailscale ip -4` printing nothing at boot: exit so systemd retries,
    rather than come up on loopback where no PC can reach it, forever."""
    from agent.__main__ import host_from_env

    monkeypatch.setenv("CROSSDROP_HOST", "")
    with pytest.raises(SystemExit):
        host_from_env()
    monkeypatch.setenv("CROSSDROP_HOST", "100.64.0.1")
    assert host_from_env() == "100.64.0.1"
    monkeypatch.delenv("CROSSDROP_HOST")
    assert host_from_env() is None
