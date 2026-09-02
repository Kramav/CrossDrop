"""Run: pytest.

The external-control surface — what a program needs that a person does not:
capability discovery instead of collecting 501s, per-screen results instead of
one url, typed errors instead of English, and a client you can construct rather
than only configure.
"""

import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

import roomctl
from agent import app as appmod
from agent import browser
from agent.app import app

TOKEN = "test-token"
H = {"Authorization": f"Bearer {TOKEN}"}


def write_config(tmp_path, kind="chromium", names=("left", "right")):
    blocks = "".join(f'[[screen]]\nname = "{n}"\n' for n in names)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'token = "{TOKEN}"\nhome_url = "about:blank"\n{blocks}'
                   f'[browser]\nkind = "{kind}"\nautolaunch = false\n',
                   encoding="utf-8")
    return cfg


@pytest.fixture
def two_screens(tmp_path, monkeypatch):
    """A two-monitor chromium agent in-process, with no browser behind it."""
    monkeypatch.setenv("ROOM_CONFIG", str(write_config(tmp_path)))
    with TestClient(app) as client:
        yield client


@pytest.fixture
def live(request, tmp_path, monkeypatch):
    """The same agent on a real socket. Yields its base url.

    roomctl talks HTTP, so the transport has to be real for its tests to mean
    anything; `two_screens` above is enough for everything server-side.
    """
    kind = getattr(request, "param", "chromium")
    monkeypatch.setenv("ROOM_CONFIG", str(write_config(tmp_path, kind=kind)))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    server.should_exit = True
    thread.join(10)


# --- capability discovery ---------------------------------------------------

def test_status_says_which_half_of_the_api_exists(two_screens):
    """Without this a caller learns what scroll does by calling it and reading
    the 501, which is not a thing a program can do."""
    s = two_screens.get("/v1/status", headers=H).json()
    assert s["kind"] == "chromium"
    assert {"navigate", "scroll", "autoscroll", "media", "screens"} <= set(s["supports"])
    assert s["started_at"] > 0        # restart detection for a polling controller


def test_firefox_advertises_only_what_it_can_do():
    # The dev box ships firefox and the Pi ships chromium, so these really are
    # two different APIs and `supports` is the only thing that says so.
    assert browser.supports({"browser": {"kind": "firefox"}}) == ["navigate"]
    assert "media" in browser.supports({"browser": {"kind": "chromium"}})


def test_every_cdp_only_capability_has_a_guard():
    """SUPPORTS is hand-maintained beside the NotImplementedErrors it describes.
    If a capability is listed for chromium and withheld from firefox, calling it
    on firefox must actually raise — or `supports` is lying."""
    cfg = {"browser": {"kind": "firefox", "debug_port": 9222},
           "screens": [{"name": "left", "position": "", "home_url": "about:blank"},
                       {"name": "right", "position": "", "home_url": "about:blank"}]}
    withheld = set(browser.supports({"browser": {"kind": "chromium"}})) - \
        set(browser.supports(cfg))
    assert withheld == {"scroll", "autoscroll", "media", "screens"}
    for call in (lambda: browser.scroll(cfg, "left", dy=1),
                 lambda: browser.media(cfg, "left"),
                 lambda: browser.navigate(cfg, "https://x/", "right")):
        with pytest.raises(NotImplementedError):
            call()


def test_firefox_refuses_a_screen_it_cannot_reach():
    """The bug this replaces: `screen` was ignored on the BiDi path, so asking
    for the second monitor drove the first one and reported success."""
    cfg = {"browser": {"kind": "firefox", "debug_port": 9222},
           "screens": [{"name": "left", "position": "", "home_url": "about:blank"},
                       {"name": "right", "position": "", "home_url": "about:blank"}]}
    with pytest.raises(NotImplementedError, match="right"):
        browser.navigate(cfg, "https://x/", "right")
    with pytest.raises(NotImplementedError, match="right"):
        browser.current_url(cfg, "right")


# --- per-screen fan-out -----------------------------------------------------

def only_left_works(monkeypatch, working=("left",)):
    def nav(cfg, url, screen=None):
        if screen not in working:
            raise OSError(f"{screen}: window gone")
        return url
    monkeypatch.setattr(appmod.browser, "navigate", nav)


def test_all_reports_each_screen_separately(two_screens, monkeypatch):
    only_left_works(monkeypatch)
    r = two_screens.post("/v1/navigate", headers=H,
                         json={"url": "https://x/", "screen": "all"})
    assert r.status_code == 200
    body = r.json()
    # The whole point: a half-succeeded fan-out is visible as such. Before this
    # it was a 503 with some monitors already changed and no way to tell which.
    assert body["ok"] is False
    assert {s["name"]: s["ok"] for s in body["screens"]} == {"left": True, "right": False}
    assert "window gone" in next(s["error"] for s in body["screens"] if not s["ok"])
    assert body["current_url"] == "https://x/"      # the last one that worked


def test_all_succeeding_still_looks_like_it_always_did(two_screens, monkeypatch):
    only_left_works(monkeypatch, working=("left", "right"))
    body = two_screens.post("/v1/navigate", headers=H,
                            json={"url": "https://x/", "screen": "all"}).json()
    assert body["ok"] is True and body["current_url"] == "https://x/"
    assert all(s["ok"] for s in body["screens"])


def test_a_single_named_screen_still_503s(two_screens, monkeypatch):
    """One screen, one verdict. Every client written before this handles it."""
    only_left_works(monkeypatch)
    r = two_screens.post("/v1/navigate", headers=H,
                         json={"url": "https://x/", "screen": "right"})
    assert r.status_code == 503


def test_all_screens_failing_is_still_a_503(two_screens, monkeypatch):
    """Nothing happened, so nothing is what we report. A 200 with ok:false here
    would mean a dead browser looked like a partial success."""
    only_left_works(monkeypatch, working=())
    r = two_screens.post("/v1/navigate", headers=H,
                         json={"url": "https://x/", "screen": "all"})
    assert r.status_code == 503


def test_home_and_reload_fan_out_the_same_way(two_screens, monkeypatch):
    only_left_works(monkeypatch)
    monkeypatch.setattr(appmod.browser, "current_url", lambda *a, **k: "https://was/")
    for path in ("/v1/home", "/v1/reload"):
        body = two_screens.post(path, headers=H, json={"screen": "all"}).json()
        assert body["ok"] is False, path
        assert [s["name"] for s in body["screens"]] == ["left", "right"], path


# --- upload staging ---------------------------------------------------------

def test_upload_can_stage_without_putting_it_on_the_wall(two_screens, monkeypatch):
    shown = []
    monkeypatch.setattr(appmod.browser, "navigate",
                        lambda cfg, url, screen=None: shown.append(url) or url)
    r = two_screens.post("/v1/upload", headers=H,
                         files={"file": ("note.txt", b"hello", "text/plain")},
                         data={"navigate": "false"})
    assert r.status_code == 200 and r.json()["url"].startswith("/files/")
    assert shown == []                  # staged, not displayed

    two_screens.post("/v1/upload", headers=H,
                     files={"file": ("note.txt", b"hello", "text/plain")})
    assert len(shown) == 1              # default is unchanged: upload still shows


# --- the client -------------------------------------------------------------

def test_client_needs_no_targets_file(live, tmp_path, monkeypatch):
    """The actual Phase 7 blocker: a program holding a url and a token had to
    write TOML to disk before it could use this library."""
    monkeypatch.setenv("ROOMCTL_TARGETS", str(tmp_path / "does-not-exist.toml"))
    with roomctl.Client(live, TOKEN) as c:
        assert c.status()["up"] is True


@pytest.mark.parametrize("live", ["firefox"], indirect=True)
def test_501_arrives_as_unsupported(live):
    with roomctl.Client(live, TOKEN) as c, pytest.raises(roomctl.Unsupported) as e:
        c.scroll()
    assert e.value.status == 501
    assert "CDP" in e.value.detail
    # Still a RuntimeError, so nothing that predates the typed errors breaks.
    assert isinstance(e.value, RuntimeError)


def test_503_arrives_as_unavailable(live):
    with roomctl.Client(live, TOKEN) as c, pytest.raises(roomctl.Unavailable) as e:
        c.navigate("https://example.com")
    assert e.value.status == 503


def test_a_box_that_is_off_is_not_the_same_as_a_box_that_said_no():
    # Nothing listening on port 1. This is the case a controller most needs to
    # tell apart, and it used to be a bare RuntimeError like everything else.
    with roomctl.Client("http://127.0.0.1:1", TOKEN, timeout=2) as c, \
            pytest.raises(roomctl.Unreachable) as e:
        c.status()
    assert e.value.status == 0


def test_the_named_target_api_is_unchanged(live, tmp_path, monkeypatch):
    targets = tmp_path / "targets.toml"
    targets.write_text(f'[study]\nurl = "{live}"\ntoken = "{TOKEN}"\n', encoding="utf-8")
    monkeypatch.setenv("ROOMCTL_TARGETS", str(targets))
    assert roomctl.status()["up"] is True
    assert roomctl.status("study")["up"] is True
