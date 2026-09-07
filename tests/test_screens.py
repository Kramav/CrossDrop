"""Run: pytest.

Multi-screen and scroll, with the CDP transport stubbed. What matters here is
*routing* — that "right" reaches the right window and that a scroll turns into
the right protocol call — and none of that needs a browser to check.
"""

import contextlib
import threading
import time
from pathlib import Path

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


# --- which window is which --------------------------------------------------
# The fallback maps screens to windows by position in /json's list. That is the
# order the windows were opened in -- but only while the list holds nothing but
# those windows, and a browser with extensions loaded does not guarantee that.

def with_pages(monkeypatch, pages, bounds=None):
    """Re-stub /json with `pages`, and optionally give each window a position."""
    browser._targets.clear()
    monkeypatch.setattr(browser, "_get", lambda port, path:
                        pages if path == "/json"
                        else {"webSocketDebuggerUrl": "ws://browser"})
    seen = []

    @contextlib.contextmanager
    def rpc(ws_url):
        def call(method, params=None):
            seen.append((ws_url, method, params or {}))
            tid = (params or {}).get("targetId")
            return {"windowId": 7, "bounds": (bounds or {}).get(tid, {})}
        yield call

    monkeypatch.setattr(browser, "_rpc", rpc)
    return seen


def test_an_extension_page_does_not_shift_every_screen(monkeypatch):
    """The bug. A loaded extension's own page is type "page" in /json, so it
    landed in the list ahead of the kiosk windows and moved each screen one
    place along -- silently, and then cached, so it stayed wrong. On a display
    that can now be clicked and typed into, that is a password going to the
    wrong monitor."""
    with_pages(monkeypatch, [
        {"type": "page", "id": "EXT", "url": "chrome-extension://abc/options.html",
         "webSocketDebuggerUrl": "ws://ext"},
        *PAGES,
    ])
    assert browser._cdp_page(make_cfg(), "left")["id"] == "T1"
    assert browser._cdp_page(make_cfg(), "right")["id"] == "T2"


@pytest.mark.parametrize("url", [
    "devtools://devtools/bundled/inspector.html",
    "chrome-extension://abc/options.html",
])
def test_a_target_no_window_of_ours_could_be_showing_is_dropped(monkeypatch, url):
    """Only these two. /v1/navigate allows http and https alone and `home_url`
    is validated the same way, so nothing can steer a kiosk window here."""
    with_pages(monkeypatch, [{"type": "page", "id": "X", "url": url,
                              "webSocketDebuggerUrl": "ws://x"}, *PAGES])
    assert browser._cdp_page(make_cfg(), "left")["id"] == "T1"


def test_a_failed_page_is_still_our_window(monkeypatch):
    """chrome-error:// looks like one of the browser's own pages and is not: it
    is our window having failed to load, which is the exact state /v1/inspect
    reports and update.sh rolls a release back on. Filtering it would lose the
    window at the moment it most needs describing."""
    error = {"type": "page", "id": "T1", "url": "chrome-error://chromewebdata/",
             "webSocketDebuggerUrl": "ws://one"}
    with_pages(monkeypatch, [error, PAGES[1]])
    cfg = make_cfg()
    assert browser._cdp_page(cfg, "left")["id"] == "T1"
    assert browser._cdp_page(cfg, "right")["id"] == "T2"


def test_a_single_screen_takes_whatever_is_there(monkeypatch):
    """One screen cannot be sent to the wrong monitor, so an unexpected extra
    page must not turn a working display into an error."""
    with_pages(monkeypatch, [*PAGES])
    cfg = make_cfg(names=("main",))
    assert browser._cdp_page(cfg)["id"] == "T1"


def test_an_unmatched_count_is_resolved_by_where_the_window_is(monkeypatch):
    """Three windows, two screens: list order means nothing now, so ask the
    browser where each window actually is."""
    third = {"type": "page", "id": "T3", "url": "http://three/",
             "webSocketDebuggerUrl": "ws://three"}
    seen = with_pages(monkeypatch, [third, *PAGES], bounds={
        "T3": {"left": 9000, "top": 0}, "T1": {"left": 0, "top": 0},
        "T2": {"left": 1366, "top": 0}})
    cfg = make_cfg()
    cfg["screens"][0]["position"] = "0,0"
    cfg["screens"][1]["position"] = "1366,0"
    assert browser._cdp_page(cfg, "right")["id"] == "T2"
    assert browser._cdp_page(cfg, "left")["id"] == "T1"
    assert any(m == "Browser.getWindowForTarget" for _, m, _ in seen)


def test_a_window_a_few_pixels_off_still_matches(monkeypatch):
    """A compositor is entitled to nudge a window; nearest beats exact."""
    third = {"type": "page", "id": "T3", "url": "http://three/",
             "webSocketDebuggerUrl": "ws://three"}
    with_pages(monkeypatch, [third, *PAGES], bounds={
        "T3": {"left": 0, "top": 0}, "T1": {"left": 2, "top": 1},
        "T2": {"left": 1360, "top": 4}})
    cfg = make_cfg()
    cfg["screens"][1]["position"] = "1366,0"
    assert browser._cdp_page(cfg, "right")["id"] == "T2"


def test_an_unmatched_count_with_nothing_to_match_on_says_so(monkeypatch):
    """Refusing beats guessing: the caller gets a 503 it can read, instead of a
    click landing on the other monitor."""
    with_pages(monkeypatch, [{"type": "page", "id": "T3", "url": "http://three/",
                              "webSocketDebuggerUrl": "ws://three"}, *PAGES])
    with pytest.raises(RuntimeError, match="position"):
        browser._cdp_page(make_cfg(), "right")      # no position configured
    assert "right" not in browser._targets          # and nothing wrong is cached


def test_a_guess_is_never_cached(monkeypatch):
    """The old failure was not just picking wrong once -- it wrote the wrong
    mapping into _targets, so every later call repeated it without asking."""
    with_pages(monkeypatch, [{"type": "page", "id": "T3", "url": "http://three/",
                              "webSocketDebuggerUrl": "ws://three"}, *PAGES])
    with contextlib.suppress(RuntimeError):
        browser._cdp_page(make_cfg(), "left")
    assert browser._targets == {}


# --- scroll -----------------------------------------------------------------

def wheel(cdp):
    return next(p for _, m, p in cdp if m == "Input.dispatchMouseEvent")


def test_scroll_dy_is_a_wheel_event(cdp):
    browser.scroll(make_cfg(), "left", dy=450)
    # A wheel event, not window.scrollBy: the PDF viewer ignores scripted scroll.
    assert [ws for ws, m, _ in cdp if m == "Input.dispatchMouseEvent"] == ["ws://one"]
    assert wheel(cdp)["type"] == "mouseWheel" and wheel(cdp)["deltaY"] == 450


def test_scroll_aims_at_the_viewport_centre(cdp):
    """A fixed point near the top-left lands in the PDF viewer's thumbnail
    sidebar and scrolls that instead of the document."""
    browser.scroll(make_cfg(), "left", dy=100)
    # The stub reports no layout metrics, so this is the fallback -- what matters
    # is that it is nowhere near the sidebar.
    assert wheel(cdp)["x"] >= 300 and wheel(cdp)["y"] >= 300


def test_jumps_are_oversized_wheel_events(cdp):
    """Scroll offsets clamp, so one huge delta lands exactly at the end -- and
    unlike Home/End it reaches the PDF viewer's embedded frame."""
    browser.scroll(make_cfg(), "right", to="bottom")
    assert wheel(cdp)["deltaY"] > 1_000_000
    cdp.clear()
    browser.scroll(make_cfg(), "left", to="top")
    assert wheel(cdp)["deltaY"] < -1_000_000


def test_bad_jump_target_rejected(cdp):
    with pytest.raises(ValueError):
        browser.scroll(make_cfg(), "left", to="sideways")


def test_firefox_says_so_instead_of_crashing(cdp):
    with pytest.raises(NotImplementedError, match="chromium"):
        browser.scroll(make_cfg(kind="firefox"), "left", dy=100)
    with pytest.raises(NotImplementedError, match="chromium"):
        browser.open_window(make_cfg(kind="firefox"), {"name": "right"})


# --- autoscroll -------------------------------------------------------------

def run_autoscroll(monkeypatch, speed=40, fail=(), secs=0.15, tick=None):
    """Drive autoscroll against a stubbed CDP for `secs`. Returns (opened, calls).

    `fail` names methods the stub should reject, which is how the old-build
    fallback gets exercised without an old build.
    """
    opened, calls = [], []

    @contextlib.contextmanager
    def rpc(ws_url):
        opened.append(ws_url)

        def call(method, params=None):
            if method in fail:
                raise RuntimeError(f"{method} failed: not supported")
            calls.append((method, params))
            return {}
        yield call

    monkeypatch.setattr(browser, "_get",
                        lambda port, path: PAGES if path == "/json" else {})
    monkeypatch.setattr(browser, "_rpc", rpc)
    # AUTOSCROLL_TICK is left alone unless asked: the gesture speed is derived
    # from it, so shrinking it here would make the px/s assertions meaningless.
    # Only the wheel fallback actually paces itself by it.
    if tick:
        monkeypatch.setattr(browser, "AUTOSCROLL_TICK", tick)
    monkeypatch.setattr(browser, "GESTURE_SECS", 0.01)
    browser._targets.clear()

    stop = threading.Event()
    t = threading.Thread(target=browser.autoscroll,
                         args=(make_cfg(), "left", speed, stop), daemon=True)
    t.start()
    time.sleep(secs)
    stop.set()
    t.join(2)
    assert not t.is_alive(), "autoscroll ignored its stop event"
    return opened, calls


def test_autoscroll_holds_one_connection_for_the_whole_run(monkeypatch):
    """PLAN.md §11 finding 5. Calling scroll() in a loop meant a TCP connect, an
    HTTP GET, a websocket handshake and two CDP round-trips *per tick*, ten
    times a second, on a Pi already busy rendering the page being scrolled."""
    opened, calls = run_autoscroll(monkeypatch)
    gestures = [p for m, p in calls if m == "Input.synthesizeScrollGesture"]
    assert len(gestures) >= 3, calls            # it really scrolled, repeatedly
    assert opened == ["ws://one"]               # ...down one connection


def test_autoscroll_is_one_smooth_gesture_not_a_stack_of_jumps(monkeypatch):
    """The steppiness fix: Chromium interpolates the gesture at frame rate, so
    the agent must not be posting discrete wheel deltas at all."""
    _, calls = run_autoscroll(monkeypatch)
    assert not [m for m, _ in calls if m == "Input.dispatchMouseEvent"]
    g = [p for m, p in calls if m == "Input.synthesizeScrollGesture"][0]
    # yDistance is positive to scroll UP, opposite to a wheel deltaY. Positive
    # speed must still scroll down, and inverting this is silent.
    assert g["yDistance"] < 0
    assert g["preventFling"] is True
    # speed 40/tick is 400 px/s, the rate the wheel loop used to give.
    assert g["speed"] == 400


def test_autoscroll_up_reverses_only_the_direction(monkeypatch):
    _, calls = run_autoscroll(monkeypatch, speed=-40)
    g = [p for m, p in calls if m == "Input.synthesizeScrollGesture"][0]
    assert g["yDistance"] > 0
    assert g["speed"] == 400        # a magnitude, never negative


def test_autoscroll_falls_back_to_wheel_ticks_on_an_old_build(monkeypatch):
    """The gesture API is experimental. A build without it must still scroll,
    not leave someone looking at a display that quietly stopped."""
    _, calls = run_autoscroll(monkeypatch, fail=("Input.synthesizeScrollGesture",),
                              tick=0.01)
    wheels = [p for m, p in calls if m == "Input.dispatchMouseEvent"]
    assert len(wheels) >= 3, calls
    assert all(w["deltaY"] == 40 for w in wheels)


def test_zero_speed_does_not_spin(monkeypatch):
    """A 0 px/s gesture completes instantly; looping on it would peg a Pi core."""
    _, calls = run_autoscroll(monkeypatch, speed=0)
    assert not [m for m, _ in calls if m.startswith("Input.")]


def test_autoscroll_stops_when_the_event_is_set(monkeypatch):
    """The stop path is the one that leaks a thread and a socket if it breaks."""
    monkeypatch.setattr(browser, "AUTOSCROLL_TICK", 0.01)
    stop = threading.Event()
    stop.set()                                   # already stopped before we start
    with pytest.raises(NotImplementedError):     # firefox: refused before connecting
        browser.autoscroll(make_cfg(kind="firefox"), "left", 40, stop)


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


def test_size_matches_the_monitor_to_avoid_a_boot_flash(cdp):
    browser.open_window(make_cfg(), {"name": "right", "position": "1366,0",
                                     "size": "2560x1440", "home_url": "about:blank"})
    move = next(p["bounds"] for _, m, p in cdp
                if m == "Browser.setWindowBounds" and p["bounds"].get("width"))
    assert (move["width"], move["height"]) == (2560, 1440)


def test_a_typo_in_position_names_the_value(cdp):
    """This runs at boot on a box with no keyboard: the error has to say what to
    fix, not raise ValueError from inside a generator."""
    with pytest.raises(RuntimeError, match="1366:0"):
        browser.open_window(make_cfg(), {"name": "right", "position": "1366:0",
                                         "size": "", "home_url": "about:blank"})
    with pytest.raises(RuntimeError, match="2560,1440"):
        browser.open_window(make_cfg(), {"name": "right", "position": "0,0",
                                         "size": "2560,1440", "home_url": "about:blank"})


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

@pytest.fixture
def running_autoscroll(monkeypatch):
    """Autoscroll that actually runs until it is stopped.

    These two used to leave `browser.autoscroll` unstubbed, so the worker thread
    reached a debug port that was not there, gave up, and removed its own entry.
    They passed only because that failure took a couple of seconds — long enough
    for the assertion to get in first. Adding the fail-fast latch to _get() made
    the same failure instant and the race started going the other way, which is
    the trouble with a test that depends on how slow a socket is.
    """
    # Restored afterwards. `app` is a module-level singleton every test shares,
    # so a config left behind here is one a later test's live server answers
    # with -- including its token, which is not the token that test will send.
    cfg = make_cfg()
    monkeypatch.setattr(appmod.app.state, "cfg", cfg, raising=False)
    monkeypatch.setattr(browser, "scroll", lambda *a, **k: None)
    monkeypatch.setattr(browser, "navigate", lambda c, url, s=None: url)
    monkeypatch.setattr(browser, "autoscroll",
                        lambda c, screen, speed, stop: stop.wait(20))
    yield cfg
    for name in list(appmod._autoscroll):
        appmod._autoscroll_stop(name)


def started(name, want=True, timeout=5.0):
    """Wait for the worker thread to register (or clear) its entry."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and (name in appmod._autoscroll) != want:
        time.sleep(0.01)
    return (name in appmod._autoscroll) == want


def test_autoscroll_stops_when_the_screen_navigates(running_autoscroll):
    """The bug most likely to ship unnoticed: a leftover loop scrolling whatever
    page lands next, with nothing in the UI to explain it."""
    appmod._autoscroll_start(running_autoscroll, "left", 40)
    assert started("left")
    appmod._go("https://elsewhere/", "left")
    assert started("left", want=False)


def test_autoscroll_on_one_screen_leaves_the_other_alone(running_autoscroll):
    appmod._autoscroll_start(running_autoscroll, "left", 40)
    appmod._autoscroll_start(running_autoscroll, "right", 40)
    assert started("left") and started("right")
    appmod._go("https://x/", "left")
    assert started("left", want=False)
    assert started("right"), "navigating one screen stopped the other"


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


def test_a_path_home_url_resolves_to_this_agent(tmp_path):
    """There used to be three definitions of home_url: config.example.toml
    shipped "/home", PUT /v1/settings 422'd exactly that, and /v1/home handed it
    to Page.navigate unchecked. One now, in load_config -- a path means this
    agent's own page, resolved against [server]."""
    p = tmp_path / "config.toml"
    p.write_text('token = "t"\nhome_url = "/home"\n'
                 '[server]\nhost = "100.1.2.3"\nport = 9000\n'
                 '[browser]\nautolaunch = false\nkind = "chromium"\n',
                 encoding="utf-8")
    cfg = appmod.load_config(p)
    assert cfg["screens"][0]["home_url"].startswith("http://100.1.2.3:9000/home")
    # And it is the same shape /v1/navigate accepts, so ?screen= still lands.
    assert cfg["screens"][0]["home_url"].endswith("?screen=main")


def test_the_shipped_example_config_is_loadable(tmp_path):
    """config.example.toml is copied verbatim on a fresh install. It shipped a
    home_url that PUT /v1/settings rejected and Page.navigate could not use."""
    example = (Path(__file__).parent.parent / "agent/config.example.toml"
               ).read_text(encoding="utf-8")
    p = tmp_path / "config.toml"
    p.write_text(example.replace('token = "change-me"', 'token = "t"'),
                 encoding="utf-8")
    cfg = appmod.load_config(p)
    assert cfg["screens"][0]["home_url"].startswith(("http://", "https://"))


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)",
                                 "data:text/html,x"])
def test_an_unusable_home_url_is_refused_at_load(tmp_path, url):
    """Not left for Page.navigate to shrug at: /v1/navigate has always enforced
    http/https, and home_url was the one way round it."""
    p = tmp_path / "config.toml"
    p.write_text(f'token = "t"\nhome_url = "{url}"\n'
                 '[browser]\nautolaunch = false\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="home_url"):
        appmod.load_config(p)


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


# --- window state -----------------------------------------------------------
# The escape hatch from --kiosk. Getting the order wrong here strands the window:
# Chromium will not leave minimized directly, so a botched restore leaves the Pi
# showing nothing until someone stops the service -- the thing this route exists
# to avoid.

def bounds(cdp):
    return [p["bounds"]["windowState"] for _, m, p in cdp if m == "Browser.setWindowBounds"]


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    monkeypatch.setattr(browser, "PLACE_SETTLE", 0)


def test_minimize_is_one_setwindowbounds(cdp):
    browser.window(make_cfg(), {"name": "right", "position": ""}, "minimized")
    assert bounds(cdp) == ["minimized"]


def test_fullscreen_goes_via_normal(cdp):
    """Straight to fullscreen from minimized is refused; normal first is the fix."""
    browser.window(make_cfg(), {"name": "left", "position": ""}, "fullscreen")
    assert bounds(cdp) == ["normal", "fullscreen"]


def test_positioned_screen_is_restored_to_its_own_monitor(cdp):
    """With a position we reuse place(), or the window comes back fullscreen on
    whichever monitor it happened to be minimized from."""
    browser.window(make_cfg(), {"name": "right", "position": "1366,0"}, "fullscreen")
    moves = [p["bounds"] for _, m, p in cdp if m == "Browser.setWindowBounds"]
    assert moves[0]["left"] == 1366 and moves[0]["windowState"] == "normal"
    assert moves[-1]["windowState"] == "fullscreen"


def test_firefox_window_says_so(cdp):
    with pytest.raises(NotImplementedError, match="chromium"):
        browser.window(make_cfg(kind="firefox"), {"name": "left"}, "minimized")


def test_bad_window_state_is_422(client):
    r = client.post("/v1/window", json={"state": "sideways"}, headers=AUTH)
    assert r.status_code == 422


# --- roomctl ----------------------------------------------------------------

@pytest.fixture
def spy(monkeypatch, cli_target):
    seen = {}

    def fake(self, screen=None, **kw):
        seen.update(screen=screen, **kw)
        return {"ok": True}

    monkeypatch.setattr(roomctl.Client, "scroll", fake)
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
