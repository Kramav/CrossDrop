"""Run: pytest.

The agent surviving things, as opposed to doing things. Every test here is a
regression for a failure whose only cure was walking into the room and plugging
a keyboard into a box that has no keyboard -- which is the reason this file is
separate from test_agent.py rather than folded into it.
"""

import logging
import threading
import time

import pytest
from fastapi.testclient import TestClient

from agent import app as appmod
from agent import display
from agent.app import app

TOKEN = "test-token"
H = {"Authorization": f"Bearer {TOKEN}"}


def write_config(tmp_path, autolaunch=False, names=("left", "right")):
    blocks = "".join(f'[[screen]]\nname = "{n}"\n' for n in names)
    p = tmp_path / "config.toml"
    p.write_text(f'token = "{TOKEN}"\nhome_url = "about:blank"\n{blocks}'
                 f'[browser]\nkind = "chromium"\n'
                 f'autolaunch = {str(autolaunch).lower()}\n', encoding="utf-8")
    return p


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CROSSDROP_CONFIG", str(write_config(tmp_path)))
    with TestClient(app) as c:
        yield c


def until(predicate, timeout=5.0):
    """Wait for a background thread to get somewhere. Returns what it found, so
    a failed wait fails on the assertion that follows rather than here."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def no_browser(monkeypatch):
    """A browser that is absent *quickly*.

    Without this every /v1/status in this file spends browser._get's 5s socket
    timeout per screen discovering what the test already knows. The failure mode
    is identical -- an OSError out of current_url -- so nothing is being papered
    over here except the wait. That the wait itself is unbounded is a separate
    finding, and not one this file is about.
    """
    def dead(cfg, screen=None):
        raise OSError("[Errno 111] Connection refused")

    monkeypatch.setattr(appmod.browser, "current_url", dead)


# --- a launch that fails must not take the API with it ----------------------

def test_status_answers_when_the_browser_will_not_launch(tmp_path, monkeypatch,
                                                         no_browser):
    """The failure this prevents: browser.launch() raised inside lifespan, so
    uvicorn exited, systemd restarted us, and the next attempt failed the same
    way -- a restart loop in which /v1/status, the only thing that could have
    named the cause, was down for every attempt.
    """
    monkeypatch.setenv("CROSSDROP_CONFIG", str(write_config(tmp_path, autolaunch=True)))

    def no_binary(cfg):
        raise RuntimeError("no chromium binary found; set browser.path in config")

    monkeypatch.setattr(appmod.browser, "launch", no_binary)

    with TestClient(app) as c:
        assert until(lambda: appmod.app.state.launch_error)
        r = c.get("/v1/status", headers=H)
        assert r.status_code == 200             # the whole point: it answers
        s = r.json()
        assert s["up"] is True and s["browser"] == "down"
        # And says why, in the reply -- not only in a journal on a box whose
        # whole problem is that you cannot get to it.
        assert "no chromium binary" in s["error"]
        # The launch is the better explanation, so it beats the socket error
        # from a port nothing ever got as far as opening.
        assert "Connection refused" not in s["error"]


def test_a_failed_launch_keeps_retrying(tmp_path, monkeypatch):
    """The transient case, and the reason this retries rather than giving up
    once: the compositor may simply not be up yet. Before the fix that was
    covered by systemd restarting the whole agent; it has to stay covered."""
    monkeypatch.setenv("CROSSDROP_CONFIG", str(write_config(tmp_path, autolaunch=True)))
    monkeypatch.setattr(appmod, "_home_when_ready", lambda cfg: None)
    attempts = []

    def flaky(cfg):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("debug port 9222 never came up")
        return "a process, as far as this test is concerned"

    monkeypatch.setattr(appmod.browser, "launch", flaky)
    monkeypatch.setattr(appmod.browser, "stop", lambda cfg, proc: None)
    # Stand in for the browser the fake launch did not start, so `error` reports
    # the launch and nothing else.
    monkeypatch.setattr(appmod.browser, "current_url",
                        lambda cfg, screen=None: "about:blank")
    # The real backoff starts at 5s and doubles; nothing here tests the
    # arithmetic, only that a second and third attempt happen at all.
    monkeypatch.setattr(appmod, "LAUNCH_RETRY_SECS", 0.01)

    with TestClient(app) as c:
        assert until(lambda: len(attempts) >= 3)
        # Recovered on its own, with no restart, and says so.
        assert until(lambda: not c.get("/v1/status", headers=H).json()["error"])
        assert c.get("/v1/status", headers=H).json()["browser"] == "ok"


def test_a_dead_external_home_page_still_leaves_every_screen_navigated(monkeypatch):
    """_home_when_ready waited on the first http home_url of *any* host, then
    returned without navigating if it never answered. So one screen whose home
    was an external site being down left the *other* screen -- the one whose
    home is this agent's own /home -- parked on the "can't be reached" page this
    function exists to prevent, with no retry after.

    Now the wait is best-effort and only ever for our own page.
    """
    cfg = {"server": {"host": "127.0.0.1", "port": 8080},
           "display": {"restore_within_minutes": 0},
           "screens": [{"name": "left", "home_url": "http://192.0.2.1/dashboard"},
                       {"name": "right", "home_url": "http://127.0.0.1:8080/home"}]}
    sent, tries = [], []
    monkeypatch.setattr(appmod.display, "claim", lambda: True)
    monkeypatch.setattr(appmod.browser, "navigate",
                        lambda cfg, url, screen: sent.append((screen, url)))

    def refuse(url, timeout=None):
        tries.append(url)
        raise appmod.httpx.ConnectError("nothing listening")

    monkeypatch.setattr(appmod.httpx, "get", refuse)
    monkeypatch.setattr(appmod.time, "sleep", lambda s: None)   # no real minute

    appmod._home_when_ready(cfg)
    assert len(tries) == 60, "gave up early, or stopped waiting altogether"
    assert sent == [("left", "http://192.0.2.1/dashboard"),
                    ("right", "http://127.0.0.1:8080/home")]


def test_the_probe_is_our_own_page_not_whatever_sorts_first(monkeypatch):
    """The docstring always said "wait for the port". It waited for a host that
    could be anybody's -- on the Pi, an unreachable dashboard would have been
    probed sixty times while the agent's own port came up in milliseconds."""
    cfg = {"server": {"host": "127.0.0.1", "port": 8080},
           "display": {"restore_within_minutes": 0},
           "screens": [{"name": "left", "home_url": "http://192.0.2.1/dashboard"},
                       {"name": "right", "home_url": "http://10.0.0.5:8080/home"}]}
    probed = []
    monkeypatch.setattr(appmod.display, "claim", lambda: True)
    monkeypatch.setattr(appmod.browser, "navigate", lambda *a: None)
    monkeypatch.setattr(appmod.httpx, "get",
                        lambda url, timeout=None: probed.append(url))
    appmod._home_when_ready(cfg)
    assert probed == ["http://10.0.0.5:8080/home"]


def test_error_falls_back_to_whatever_the_socket_said(client, no_browser):
    """No launch was attempted here (autolaunch is off), so there is no launch
    error to report -- but the browser is still absent, and saying so beats a
    blank field next to `browser: "down"`."""
    s = client.get("/v1/status", headers=H).json()
    assert s["browser"] == "down"
    assert "Connection refused" in s["error"]


def test_a_shutdown_during_launch_still_stops_the_browser(tmp_path, monkeypatch):
    """launch() blocks in wait_ready() for up to 30s. A restart landing in that
    window used to find proc still None and leave the kiosk running -- an
    orphaned fullscreen window on a box with no keyboard, which is the failure
    the whole module is arranged around."""
    monkeypatch.setenv("CROSSDROP_CONFIG", str(write_config(tmp_path, autolaunch=True)))
    launched, stopped, in_launch = threading.Event(), [], threading.Event()

    def slow_launch(cfg):
        in_launch.set()
        launched.wait(5)            # stand in for wait_ready()
        return "the kiosk process"

    monkeypatch.setattr(appmod.browser, "launch", slow_launch)
    monkeypatch.setattr(appmod.browser, "stop", lambda cfg, proc: stopped.append(proc))
    monkeypatch.setattr(appmod.browser, "close", lambda: None)
    monkeypatch.setattr(appmod, "_home_when_ready", lambda cfg: None)

    with TestClient(app):
        assert until(in_launch.is_set)
    # Shutdown has run and saw proc as None. The launch finishes into a stopping
    # agent and has to clean up after itself.
    launched.set()
    assert until(lambda: stopped == ["the kiosk process"]), "kiosk was orphaned"


def test_a_working_browser_reports_no_error(client, monkeypatch):
    """`error` is empty when there is nothing to say, so a client can treat any
    non-empty value as a real problem."""
    monkeypatch.setattr(appmod.browser, "current_url",
                        lambda cfg, screen=None: "https://x/")
    s = client.get("/v1/status", headers=H).json()
    assert s["browser"] == "ok" and s["error"] == ""


def test_two_threads_never_share_the_bidi_socket(monkeypatch):
    """Firefox hands out one BiDi session per browser and will not replace it,
    so there is exactly one websocket -- and every browser route is `def`, i.e.
    on the threadpool. Two concurrent requests sent and recv()'d on it at once:
    the id-matching loop in _connect() means one thread eats the other's reply
    and the loser blocks to its 15s timeout. `session.new` could race too, and
    the losing socket is the one Firefox never hands back.
    """
    from agent import browser

    inside, overlapped, results = [], [], []

    def call(method, params=None):
        inside.append(method)
        overlapped.extend(inside[1:])       # anything here beside us
        time.sleep(0.02)                    # long enough to be caught at it
        inside.remove(method)
        return {"echo": method}

    fake_ws = type("WS", (), {"close": lambda self: None})()
    monkeypatch.setattr(browser, "_bidi_conns", {9222: (fake_ws, call)})

    threads = [threading.Thread(
        target=lambda i=i: results.append(browser._bidi(9222, f"m{i}")))
        for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert overlapped == [], f"two calls were on the socket at once: {overlapped}"
    # And nobody got somebody else's answer.
    assert sorted(r["echo"] for r in results) == ["m0", "m1", "m2", "m3"]


# --- autoscroll restart must not orphan the run that replaced it ------------

def test_a_restarted_autoscroll_can_still_be_stopped(client, monkeypatch):
    """The orphan. A finishing run popped whatever was under its screen name --
    which, after a second start, was the *new* run's stop event. The new run
    then scrolled with nothing holding its event: `/v1/autoscroll stop` popped
    nothing, the navigate guard in _navigate_one() stopped nothing, and the
    display went on scrolling every page it was sent afterwards. Restarting the
    agent was the only cure, on a box with no keyboard.
    """
    started, finished = [], []

    def fake_autoscroll(cfg, screen, speed, stop):
        started.append(stop)
        stop.wait(20)
        finished.append(stop)

    monkeypatch.setattr(appmod.browser, "autoscroll", fake_autoscroll)
    go = {"screen": "left", "action": "start", "speed": 40}

    assert client.post("/v1/autoscroll", headers=H, json=go).status_code == 200
    assert until(lambda: len(started) == 1)

    # Second start: stops the first run and installs its own event.
    assert client.post("/v1/autoscroll", headers=H, json=go).status_code == 200
    assert until(lambda: len(started) == 2 and len(finished) == 1)
    # Let the first run's cleanup actually run -- one dict operation. This is
    # the window in which it used to delete the second run's entry.
    assert until(lambda: appmod._autoscroll.get("left") is started[1])

    # The assertion that matters: the survivor is still reachable.
    client.post("/v1/autoscroll", headers=H,
                json={"screen": "left", "action": "stop"})
    assert until(lambda: started[1].is_set()), "second autoscroll was orphaned"
    assert until(lambda: len(finished) == 2)
    assert "left" not in appmod._autoscroll


def test_a_navigate_still_stops_a_restarted_autoscroll(client, monkeypatch):
    """The same orphan seen from the route that most depends on the guard: a
    scroll left running under a new page is what makes the display look
    haunted, and it is the case a person hits without meaning to."""
    started = []
    monkeypatch.setattr(appmod.browser, "autoscroll",
                        lambda cfg, s, speed, stop: (started.append(stop),
                                                     stop.wait(20)))
    monkeypatch.setattr(appmod.browser, "navigate",
                        lambda cfg, url, screen=None: url)
    go = {"screen": "left", "action": "start", "speed": 40}
    client.post("/v1/autoscroll", headers=H, json=go)
    assert until(lambda: len(started) == 1)
    client.post("/v1/autoscroll", headers=H, json=go)
    assert until(lambda: len(started) == 2)

    client.post("/v1/navigate", headers=H,
                json={"url": "https://x/", "screen": "left"})
    assert until(lambda: started[1].is_set()), "navigate did not stop the scroll"


def test_screens_does_not_report_an_autoscroll_that_is_not_running(client, monkeypatch):
    """The dict is what /v1/screens reports, so an entry that outlives its run
    is a display that claims to be scrolling and is not."""
    monkeypatch.setattr(appmod.browser, "autoscroll",
                        lambda cfg, s, speed, stop: stop.wait(20))
    monkeypatch.setattr(appmod.browser, "current_url",
                        lambda cfg, screen=None: "https://x/")
    client.post("/v1/autoscroll", headers=H,
                json={"screen": "left", "action": "start"})
    rows = {s["name"]: s["autoscroll"] for s in
            client.get("/v1/screens", headers=H).json()}
    assert rows == {"left": True, "right": False}

    client.post("/v1/autoscroll", headers=H,
                json={"screen": "left", "action": "stop"})
    assert until(lambda: not any(
        s["autoscroll"] for s in client.get("/v1/screens", headers=H).json()))


def test_concurrent_starts_leave_exactly_one_live_run(client, monkeypatch):
    """Ten starts at once. Nine runs end; the dict must hold the tenth, not
    nothing and not a stale one."""
    live = []
    monkeypatch.setattr(appmod.browser, "autoscroll",
                        lambda cfg, s, speed, stop: (live.append(stop),
                                                     stop.wait(20),
                                                     live.remove(stop)))
    go = {"screen": "left", "action": "start", "speed": 40}
    threads = [threading.Thread(target=client.post, args=("/v1/autoscroll",),
                                kwargs={"headers": H, "json": go})
               for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)

    assert until(lambda: len(live) == 1), f"{len(live)} runs still going"
    assert appmod._autoscroll.get("left") is live[0]
    client.post("/v1/autoscroll", headers=H,
                json={"screen": "left", "action": "stop"})
    assert until(lambda: not live), "the surviving run was orphaned"


# --- the config swap ---------------------------------------------------------

class Watched(dict):
    """A dict that records what a reader would have seen after every mutation.

    Racing two threads and hoping to land in the window cannot prove this: it is
    a couple of bytecodes wide, so the race test passed just as happily with the
    bug in place. Watching every mutation instead makes it deterministic.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.seen = []

    def _snap(self):
        self.seen.append(set(self))

    def clear(self):
        super().clear()
        self._snap()

    def update(self, *a, **kw):
        super().update(*a, **kw)
        self._snap()

    def __setitem__(self, k, v):
        super().__setitem__(k, v)
        self._snap()

    def pop(self, *a):
        out = super().pop(*a)
        self._snap()
        return out


def test_the_config_swap_never_hides_a_key_that_survives_it():
    """Every browser route is `def`, so it runs on the threadpool and really can
    read app.state.cfg mid-swap. clear() then update() left it momentarily
    empty, and a reader landing there got a KeyError and a 500 out of a route
    that had done nothing wrong.
    """
    old = Watched({"token": "t", "screens": [{"name": "left"}], "gone": 1})
    fresh = {"token": "t", "screens": [{"name": "right"}]}

    appmod.swap_config(old, fresh)

    assert old == fresh                                 # the swap happened
    assert old.seen, "nothing was observed"
    # The invariant: anything present before and after is present throughout.
    survivors = {"token", "screens"}
    assert all(survivors <= s for s in old.seen), \
        f"a reader could have missed {survivors - min(old.seen, key=len)}"


# --- display power ------------------------------------------------------------

def test_a_claim_that_x_refused_is_retried(monkeypatch):
    """claim() runs while the agent is starting, which on a slow boot is before
    the session exists. An attempt lost there used to be lost for good, leaving
    the session's own blanking timeouts to sleep the monitors with nothing able
    to wake them -- the exact trap display.py exists to avoid."""
    from agent import display

    calls = []
    monkeypatch.setattr(display, "_claimed", False)
    monkeypatch.setattr(display, "_ok", True)
    monkeypatch.setattr(display, "_run", lambda argv: calls.append(argv) or None)

    assert display.claim() is False              # X refused every xset
    assert display._claimed is False
    # All four are attempted, not just up to the first refusal.
    assert len([c for c in calls if c[0] == "xset"]) >= 4

    calls.clear()
    monkeypatch.setattr(display, "_run", lambda argv: calls.append(argv) or "")
    assert display._claim_dpms() is True         # X is up now
    assert display._claimed is True


def test_the_retry_never_turns_the_display_back_on(monkeypatch):
    """The bug the split exists to avoid: re-claiming must not carry claim()'s
    power sync with it, or the tick after a deliberate `POST /v1/display off`
    would light the room straight back up."""
    from agent import display

    monkeypatch.setattr(display, "_claimed", False)
    monkeypatch.setattr(display, "_ok", True)
    monkeypatch.setattr(display, "_run", lambda argv: "")
    display.power(False)
    assert display.awake() is False

    display._claim_dpms()
    assert display.awake() is False, "re-claiming woke the display"


# --- a wedged browser must not take the agent with it ------------------------

def test_a_dead_port_is_not_dialled_again_immediately(monkeypatch):
    """The case this is for is not a browser that has *gone* — a closed port
    refuses instantly — but one wedged and still holding it, where every call
    pays the full socket timeout. A controller polling /v1/status every 15s
    stacks those up faster than they drain, one threadpool thread per screen per
    poll, and the pool is 40: a wedged browser quietly takes the agent down.
    """
    from agent import browser

    tries = []

    def slow(url, timeout=None):
        tries.append(url)
        raise OSError("timed out")

    monkeypatch.setattr(browser.urllib.request, "urlopen", slow)
    monkeypatch.setattr(browser, "_dead_until", 0.0)

    with pytest.raises(OSError):
        browser._get(9222, "/json")
    assert len(tries) == 1
    # Second call inside the cooldown: refused without touching the socket.
    with pytest.raises(OSError, match="not trying again"):
        browser._get(9222, "/json")
    assert len(tries) == 1, "it dialled the wedged port again"


def test_the_latch_lifts_once_the_browser_answers(monkeypatch):
    from agent import browser

    monkeypatch.setattr(browser, "_dead_until", time.monotonic() + 999)
    with pytest.raises(OSError):
        browser._get(9222, "/json")

    class Fake:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"[]"

    monkeypatch.setattr(browser.urllib.request, "urlopen",
                        lambda url, timeout=None: Fake())
    # wait_ready's path bypasses the latch, which is the whole point of it:
    # polling a port that is not up *yet* must stay fast during a launch.
    assert browser._get(9222, "/json", latch=False) == []
    assert browser._get(9222, "/json") == []        # and the latch is now clear


def test_waiting_for_a_launch_is_never_latched_out(monkeypatch):
    """A fast-fail here would turn wait_ready's 0.3s poll into a 5s one and
    leave the kiosk dark for five seconds after it was ready."""
    from agent import browser

    monkeypatch.setattr(browser, "_dead_until", time.monotonic() + 999)
    tries = []

    def slow(url, timeout=None):
        tries.append(url)
        raise OSError("not up yet")

    monkeypatch.setattr(browser.urllib.request, "urlopen", slow)
    with pytest.raises(OSError):
        browser._get(9222, "/json/version", latch=False)
    assert len(tries) == 1, "the latch blocked a launch poll"


# --- the token ---------------------------------------------------------------

def unicode_config(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text('token = "påssword-with-ünicode"\nhome_url = "about:blank"\n'
                 '[browser]\nkind = "chromium"\nautolaunch = false\n',
                 encoding="utf-8")
    monkeypatch.setenv("CROSSDROP_CONFIG", str(p))
    return p


def test_a_non_ascii_token_in_the_config_is_a_401_not_a_500(tmp_path, monkeypatch,
                                                            no_browser):
    """compare_digest refuses two str arguments unless *both* are pure ASCII, so
    a token with an accent in config.toml raised TypeError out of the dependency
    and surfaced as a 500 — which reads as "the agent is broken" while the real
    problem is a token nobody can ever send."""
    unicode_config(tmp_path, monkeypatch)
    with TestClient(app) as c:
        assert c.get("/v1/status", headers={"Authorization": "Bearer nope"}
                     ).status_code == 401


def test_a_token_that_cannot_be_sent_says_so_in_the_log(tmp_path, monkeypatch,
                                                        caplog):
    """An HTTP header is latin-1, so such a token cannot reach us at all: every
    request 401s forever while the config looks perfectly fine to whoever wrote
    it. Not fatal — a bad token must never stop a keyboard-less display booting
    — so the journal is where the answer has to be."""
    unicode_config(tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="crossdrop"):
        appmod.load_config()
    assert any("non-ASCII" in r.getMessage() for r in caplog.records)


def test_an_ordinary_token_says_nothing(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("CROSSDROP_CONFIG", str(write_config(tmp_path)))
    with caplog.at_level(logging.WARNING, logger="crossdrop"):
        appmod.load_config()
    assert not [r for r in caplog.records if "non-ASCII" in r.getMessage()]


# --- the access log ---------------------------------------------------------

def test_a_mutation_is_logged(client, monkeypatch, caplog):
    """There was no log at all before this: six print() calls, none of them
    about a request. "The wall showed the wrong thing at 9am" was unanswerable
    even with the journal in front of you."""
    monkeypatch.setattr(appmod.browser, "navigate",
                        lambda cfg, url, screen=None: url)
    with caplog.at_level(logging.INFO, logger="crossdrop"):
        client.post("/v1/navigate", headers=H,
                    json={"url": "https://x/", "screen": "left"})
    line = next(r for r in caplog.records if "/v1/navigate" in r.getMessage())
    assert line.levelno == logging.INFO
    assert "POST" in line.getMessage() and "200" in line.getMessage()


def test_a_failed_request_is_logged_with_its_status(client, caplog):
    """A 503 has to be as visible as a success, or the log only records the
    times nothing was wrong."""
    with caplog.at_level(logging.INFO, logger="crossdrop"):
        client.post("/v1/navigate", headers=H, json={"url": "https://x/"})
    assert any("503" in r.getMessage() for r in caplog.records
               if "/v1/navigate" in r.getMessage())


def test_reads_do_not_flood_the_log(client, caplog, no_browser):
    """A controller polls /v1/status every 15s and the kiosk polls /home-status.
    At INFO those would bury every real action under thousands of lines a day,
    on a Pi whose journal is 32M and in RAM."""
    with caplog.at_level(logging.INFO, logger="crossdrop"):
        client.get("/v1/status", headers=H)
        client.get("/home-status")
    assert not [r for r in caplog.records if "/v1/status" in r.getMessage()]

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="crossdrop"):
        client.get("/v1/status", headers=H)
    # CROSSDROP_LOG=DEBUG is the debug mode: same line, one level down.
    assert any("/v1/status" in r.getMessage() for r in caplog.records)


def test_an_unauthenticated_request_is_logged(client, caplog):
    """401s are the ones worth having a record of."""
    with caplog.at_level(logging.INFO, logger="crossdrop"):
        client.post("/v1/navigate", json={"url": "https://x/"})
    assert any("401" in r.getMessage() for r in caplog.records
               if "/v1/navigate" in r.getMessage())


def test_our_log_level_does_not_turn_on_every_library(client):
    """INFO on the root logger also enables httpx, which narrates one line per
    request -- including _home_when_ready()'s once-a-second poll. That is our
    own audit trail evicted from a 32M RAM journal by somebody else's chatter."""
    appmod.setup_logging()
    assert appmod.log.isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)


def test_setup_logging_is_safe_to_call_twice(caplog):
    """lifespan calls it on every start, and the tests start the app dozens of
    times. A second handler on root would double every line."""
    root = logging.getLogger()
    appmod.setup_logging()
    before = len(root.handlers)
    appmod.setup_logging()
    assert len(root.handlers) == before


# --- the config swap, under concurrency -------------------------------------

def test_a_lookup_in_flight_cannot_outlive_the_config_swap(monkeypatch):
    """browser.forget_targets() closed the single-threaded hole and left the
    concurrent one open.

    Every browser route is `def`, so it runs on a threadpool thread. A call that
    had already read the *old* screen list could finish its CDP round trip and
    write its answer into `_targets` after the clear -- and that entry outlived
    the swap. `_guessed` did not contain the name, so `_refuse_guess` passed and
    /v1/input typed into a window chosen under a config that no longer existed.
    Nothing re-checked it, so it never healed.
    """
    from agent import browser

    browser.forget_targets()
    pages = [{"type": "page", "id": "T1", "url": "http://a/",
              "webSocketDebuggerUrl": "ws://one"},
             {"type": "page", "id": "T2", "url": "http://b/",
              "webSocketDebuggerUrl": "ws://two"}]
    monkeypatch.setattr(browser, "_get", lambda port, path, **k:
                        list(pages) if path == "/json"
                        else {"webSocketDebuggerUrl": "ws://browser"})

    old = {"browser": {"kind": "chromium", "debug_port": 9222},
           "screens": [{"name": "left", "position": "", "home_url": "about:blank"},
                       {"name": "right", "position": "", "home_url": "about:blank"}]}
    swapped = threading.Event()

    # The swap lands between resolving the screen list and caching the answer.
    real_identify = browser._identify

    def slow_identify(cfg, name, scr, ps):
        page = real_identify(cfg, name, scr, ps)
        swapped.wait(5)
        return page

    monkeypatch.setattr(browser, "_identify", slow_identify)
    got = {}
    t = threading.Thread(target=lambda: got.update(
        page=browser._cdp_page(old, "right")), daemon=True)
    t.start()
    time.sleep(0.1)
    browser.forget_targets()        # what swap_config does
    swapped.set()
    t.join(10)

    assert got, "the lookup never finished"
    # It may return whatever it resolved -- but it must not have left that
    # answer behind for the next call to trust.
    assert browser._targets == {}, browser._targets


def test_an_autoscroll_starting_during_a_swap_is_still_stopped(monkeypatch):
    """The stop sweep snapshots `list(_autoscroll)` before the swap, so a start
    already in flight -- the route resolved a name that was valid when it
    checked -- installs under a name the new config no longer has. Nothing could
    then stop that loop but a restart: screen_of() 404s on the name, so the UI's
    stop button pops nothing. The haunted display, by a different door."""
    from agent import browser

    cfg = {"browser": {"kind": "chromium", "debug_port": 9222},
           "screens": [{"name": "left", "position": "", "home_url": "about:blank"}],
           "display": display.DEFAULTS}
    monkeypatch.setattr(appmod.app.state, "cfg", cfg, raising=False)
    monkeypatch.setattr(browser, "autoscroll",
                        lambda c, screen, speed, stop: stop.wait(20))
    appmod._autoscroll_start(cfg, "left", 40)
    assert until(lambda: "left" in appmod._autoscroll)

    fresh = dict(cfg)
    fresh["screens"] = [{"name": "centre", "position": "", "home_url": "about:blank"}]
    appmod.swap_config(cfg, fresh)
    assert "left" not in appmod._autoscroll, \
        "a loop is running under a name no screen has; only a restart stops it"
