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
    monkeypatch.setenv("ROOM_CONFIG", str(write_config(tmp_path)))
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
    monkeypatch.setenv("ROOM_CONFIG", str(write_config(tmp_path, autolaunch=True)))

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
    monkeypatch.setenv("ROOM_CONFIG", str(write_config(tmp_path, autolaunch=True)))
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
    monkeypatch.setenv("ROOM_CONFIG", str(write_config(tmp_path, autolaunch=True)))
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


# --- the access log ---------------------------------------------------------

def test_a_mutation_is_logged(client, monkeypatch, caplog):
    """There was no log at all before this: six print() calls, none of them
    about a request. "The wall showed the wrong thing at 9am" was unanswerable
    even with the journal in front of you."""
    monkeypatch.setattr(appmod.browser, "navigate",
                        lambda cfg, url, screen=None: url)
    with caplog.at_level(logging.INFO, logger="room"):
        client.post("/v1/navigate", headers=H,
                    json={"url": "https://x/", "screen": "left"})
    line = next(r for r in caplog.records if "/v1/navigate" in r.getMessage())
    assert line.levelno == logging.INFO
    assert "POST" in line.getMessage() and "200" in line.getMessage()


def test_a_failed_request_is_logged_with_its_status(client, caplog):
    """A 503 has to be as visible as a success, or the log only records the
    times nothing was wrong."""
    with caplog.at_level(logging.INFO, logger="room"):
        client.post("/v1/navigate", headers=H, json={"url": "https://x/"})
    assert any("503" in r.getMessage() for r in caplog.records
               if "/v1/navigate" in r.getMessage())


def test_reads_do_not_flood_the_log(client, caplog, no_browser):
    """A controller polls /v1/status every 15s and the kiosk polls /home-status.
    At INFO those would bury every real action under thousands of lines a day,
    on a Pi whose journal is 32M and in RAM."""
    with caplog.at_level(logging.INFO, logger="room"):
        client.get("/v1/status", headers=H)
        client.get("/home-status")
    assert not [r for r in caplog.records if "/v1/status" in r.getMessage()]

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="room"):
        client.get("/v1/status", headers=H)
    # ROOM_LOG=DEBUG is the debug mode: same line, one level down.
    assert any("/v1/status" in r.getMessage() for r in caplog.records)


def test_an_unauthenticated_request_is_logged(client, caplog):
    """401s are the ones worth having a record of."""
    with caplog.at_level(logging.INFO, logger="room"):
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
