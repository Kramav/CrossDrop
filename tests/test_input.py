"""Run: pytest.

Click, type and page inspection, with the CDP transport stubbed the way
test_media.py does it. No browser here can prove a click *lands* -- what this
proves is everything around that: the gate, the ordering, that a bad list runs
nothing at all, that a failed step stops the ones after it, and that a typed
password never reaches a log.
"""

import contextlib
import json
import logging

import pytest
from fastapi.testclient import TestClient

import roomctl
from agent import app as appmod
from agent import browser
from agent.app import app
from roomctl import cli

TOKEN = "test-token"
H = {"Authorization": f"Bearer {TOKEN}"}

PAGES = [
    {"type": "page", "id": "T1", "url": "http://one/", "title": "One",
     "webSocketDebuggerUrl": "ws://one"},
    {"type": "page", "id": "T2", "url": "http://two/", "title": "Two",
     "webSocketDebuggerUrl": "ws://two"},
]

STATE = {"title": "Sign in", "ready_state": "complete", "error_page": False,
         "has_media": False, "scroll_y": 0, "scroll_height": 2400,
         "fields": [{"selector": "#user", "tag": "input", "type": "text",
                     "label": "Username"},
                    {"selector": "#pass", "tag": "input", "type": "password",
                     "label": "Password"}]}


def make_cfg(kind="chromium", enabled=True):
    return {"token": "t", "home_url": "about:blank",
            "browser": {"kind": kind, "debug_port": 9222},
            "interact": {"enabled": enabled, "max_actions": 40,
                         "deadline_ms": 30000},
            "screens": [{"name": n, "position": "", "home_url": "about:blank"}
                        for n in ("left", "right")]}


class Calls(list):
    """Calls that went out. `found` is what _FIND_JS resolves a selector to;
    None means no such visible element."""
    found = {"x": 400, "y": 300}
    state = STATE

    def methods(self):
        return [m for _, m, _ in self]

    def params(self, method):
        return [p for _, m, p in self if m == method]


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
            if method != "Runtime.evaluate":
                return {}
            # scrollIntoView, not getBoundingClientRect: _INSPECT_JS measures
            # elements too, so the rect is not what tells the two scripts apart.
            expr = (params or {}).get("expression", "")
            value = calls.found if "scrollIntoView" in expr else calls.state
            return {"result": {"value": value}}
        yield call

    monkeypatch.setattr(browser, "_get", get)
    monkeypatch.setattr(browser, "_rpc", rpc)
    return calls


# --- inspect ----------------------------------------------------------------

def test_inspect_reports_the_page_and_its_url(cdp):
    r = browser.inspect(make_cfg(), "right")
    assert r["url"] == "http://two/" and r["title"] == "Sign in"
    assert r["ready_state"] == "complete" and r["scroll_height"] == 2400


def test_a_chrome_error_url_counts_as_an_error_page(cdp, monkeypatch):
    """Two witnesses on purpose: a page that failed hard enough may have no
    document to ask, and Chromium parks those on chrome-error://."""
    monkeypatch.setitem(PAGES[0], "url", "chrome-error://chromewebdata/")
    try:
        assert browser.inspect(make_cfg(), "left")["error_page"] is True
    finally:
        PAGES[0]["url"] = "http://one/"


def test_inspect_never_reports_a_field_value(cdp):
    """Naming a password box is how a caller knows where to type. Handing back
    what is in it would make a diagnostic route a credential leak."""
    assert "value" not in browser._INSPECT_JS
    for f in browser.inspect(make_cfg(), "left")["fields"]:
        assert set(f) <= {"selector", "tag", "type", "label"}


def test_a_script_error_is_a_failure_not_a_silent_none(cdp, monkeypatch):
    @contextlib.contextmanager
    def broken(ws_url):
        yield lambda m, p=None: {"exceptionDetails": {"text": "TypeError: nope"}}
    monkeypatch.setattr(browser, "_rpc", broken)
    with pytest.raises(RuntimeError, match="TypeError"):
        browser.inspect(make_cfg(), "left")


# --- the input gate ---------------------------------------------------------

def test_input_is_off_unless_the_config_says_otherwise():
    off = make_cfg(enabled=False)
    assert "input" not in browser.supports(off)
    assert browser.interactive(off) is False
    # Absent, not merely refused: a client reads `supports` to decide what to
    # offer, so a disabled agent has to look like one that cannot do it.
    assert "input" in browser.supports(make_cfg(enabled=True))


def test_a_config_with_no_interact_block_is_off():
    assert browser.interactive({"browser": {"kind": "chromium"}}) is False


# --- what reaches the browser -----------------------------------------------

def test_a_click_is_a_press_and_a_release(cdp):
    browser.input(make_cfg(), "left", [{"do": "click", "x": 10, "y": 20}])
    kinds = [p["type"] for p in cdp.params("Input.dispatchMouseEvent")]
    assert kinds == ["mousePressed", "mouseReleased"]
    assert all(p["x"] == 10 and p["y"] == 20 and p["button"] == "left"
               for p in cdp.params("Input.dispatchMouseEvent"))


def test_double_and_right_differ_only_where_they_should(cdp):
    browser.input(make_cfg(), "left", [{"do": "double", "x": 1, "y": 2},
                                       {"do": "right", "x": 1, "y": 2}])
    events = cdp.params("Input.dispatchMouseEvent")
    assert [e["clickCount"] for e in events[:2]] == [2, 2]
    assert [e["button"] for e in events[2:]] == ["right", "right"]


def test_a_selector_is_resolved_to_coordinates(cdp):
    r = browser.input(make_cfg(), "left", [{"do": "click", "selector": "#user"}])
    assert (r[0]["x"], r[0]["y"]) == (400, 300)     # reported back, not guessed
    assert all(p["x"] == 400 for p in cdp.params("Input.dispatchMouseEvent"))
    # The selector reaches the page as JSON, so a quote in it cannot break out.
    assert json.dumps("#user") in cdp.params("Runtime.evaluate")[0]["expression"]


def test_a_selector_scrolls_into_view_first(cdp):
    """An element below the fold has coordinates outside the viewport, and a
    click dispatched there lands on nothing while reporting success."""
    browser.input(make_cfg(), "left", [{"do": "click", "selector": "#user"}])
    assert "scrollIntoView" in cdp.params("Runtime.evaluate")[0]["expression"]


def test_a_missing_element_stops_the_sequence(cdp):
    cdp.found = None
    r = browser.input(make_cfg(), "left",
                      [{"do": "click", "selector": "#gone"},
                       {"do": "type", "text": "hunter2"}])
    assert len(r) == 1 and r[0]["ok"] is False and "#gone" in r[0]["error"]
    # The whole reason it stops: the password must not go into whatever else
    # happens to have focus.
    assert "Input.insertText" not in cdp.methods()


def test_typing_is_one_call_not_one_per_letter(cdp):
    browser.input(make_cfg(), "left", [{"do": "type", "text": "kramav"}])
    assert cdp.params("Input.insertText") == [{"text": "kramav"}]


def test_a_key_sends_down_and_up(cdp):
    browser.input(make_cfg(), "left", [{"do": "key", "key": "Enter"}])
    events = cdp.params("Input.dispatchKeyEvent")
    assert [e["type"] for e in events] == ["keyDown", "keyUp"]
    assert events[0]["windowsVirtualKeyCode"] == 13 and events[0]["text"] == "\r"


def test_a_modified_key_carries_no_text(cdp):
    """ctrl+a is a shortcut, not the letter a. Sending text with it selects all
    *and* types an "a"."""
    browser.input(make_cfg(), "left",
                  [{"do": "key", "key": "a", "modifiers": ["ctrl"]}])
    down = cdp.params("Input.dispatchKeyEvent")[0]
    assert down["modifiers"] == 2 and down["text"] == ""
    assert down["windowsVirtualKeyCode"] == ord("A")


def test_shift_is_the_exception_and_still_types(cdp):
    browser.input(make_cfg(), "left",
                  [{"do": "key", "key": "A", "modifiers": ["shift"]}])
    assert cdp.params("Input.dispatchKeyEvent")[0]["text"] == "A"


def test_a_drag_moves_between_press_and_release(cdp):
    """HTML5 drag and drop and every canvas app want to see the pointer travel;
    a single jump is routinely ignored as a stray click."""
    browser.input(make_cfg(), "left",
                  [{"do": "drag", "from": [0, 0], "to": [100, 50]}])
    events = cdp.params("Input.dispatchMouseEvent")
    assert events[0]["type"] == "mousePressed" and events[-1]["type"] == "mouseReleased"
    assert [e["type"] for e in events[1:-1]] == ["mouseMoved"] * 5
    assert (events[-1]["x"], events[-1]["y"]) == (100, 50)


def test_actions_run_in_the_order_given(cdp):
    browser.input(make_cfg(), "left",
                  [{"do": "click", "selector": "#user"},
                   {"do": "type", "text": "kramav"},
                   {"do": "key", "key": "Enter"}])
    order = [m for m in cdp.methods() if m != "Runtime.evaluate"]
    assert order == ["Input.dispatchMouseEvent", "Input.dispatchMouseEvent",
                     "Input.insertText", "Input.dispatchKeyEvent",
                     "Input.dispatchKeyEvent"]


def test_one_connection_for_the_whole_sequence(cdp):
    browser.input(make_cfg(), "left", [{"do": "click", "x": 1, "y": 1},
                                       {"do": "type", "text": "x"},
                                       {"do": "key", "key": "Tab"}])
    assert {ws for ws, _, _ in cdp} == {"ws://one"}


# --- a bad list runs nothing ------------------------------------------------

@pytest.mark.parametrize("bad", [
    {"do": "teleport"},
    {"do": "click"},                                    # no selector, no x/y
    {"do": "click", "x": -1, "y": 5},
    {"do": "type", "text": ""},
    {"do": "key", "key": "Meta+Shift+Whatever"},
    {"do": "key", "key": "a", "modifiers": ["hyper"]},
    {"do": "drag", "from": [0, 0]},
    {"do": "wait", "ms": 99999},
])
def test_a_bad_action_runs_none_of_them(cdp, bad):
    """There is no undo. A typo in action 3 must not be found out after actions
    1 and 2 have clicked something and typed into it."""
    with pytest.raises(ValueError):
        browser.input(make_cfg(), "left",
                      [{"do": "click", "x": 1, "y": 1}, bad])
    assert "Input.dispatchMouseEvent" not in cdp.methods()


def test_the_failing_action_is_named_by_index(cdp):
    with pytest.raises(ValueError, match="action 1"):
        browser.input(make_cfg(), "left",
                      [{"do": "wait", "ms": 1}, {"do": "nope"}])


def test_an_empty_list_is_refused(cdp):
    with pytest.raises(ValueError, match="no actions"):
        browser.input(make_cfg(), "left", [])


def test_the_deadline_stops_the_rest(cdp):
    import time
    r = browser.input(make_cfg(), "left",
                      [{"do": "click", "x": 1, "y": 1},
                       {"do": "type", "text": "x"}],
                      deadline=time.monotonic() - 1)     # already past
    assert r[0]["ok"] is False and "deadline" in r[0]["error"]
    assert not cdp.methods()


# --- the routes -------------------------------------------------------------

def write_config(tmp_path, kind="chromium", enabled=True):
    p = tmp_path / "config.toml"
    p.write_text(f'token = "{TOKEN}"\nhome_url = "about:blank"\n'
                 f'[[screen]]\nname = "left"\n[[screen]]\nname = "right"\n'
                 f'[browser]\nkind = "{kind}"\nautolaunch = false\n'
                 f'[interact]\nenabled = {str(enabled).lower()}\n', encoding="utf-8")
    return p


@pytest.fixture
def client(tmp_path, monkeypatch, cdp):
    monkeypatch.setenv("ROOM_CONFIG", str(write_config(tmp_path)))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def locked(tmp_path, monkeypatch, cdp):
    monkeypatch.setenv("ROOM_CONFIG", str(write_config(tmp_path, enabled=False)))
    with TestClient(app) as c:
        yield c


def test_inspect_route(client):
    r = client.get("/v1/inspect", headers=H, params={"screen": "right"})
    assert r.status_code == 200
    body = r.json()
    assert body["screen"] == "right" and body["title"] == "Sign in"
    assert [f["type"] for f in body["fields"]] == ["text", "password"]


def test_a_login_sequence_reports_every_step(client):
    r = client.post("/v1/input", headers=H, json={"actions": [
        {"do": "click", "selector": "#user"},
        {"do": "type", "text": "kramav"},
        {"do": "click", "selector": "#pass"},
        {"do": "type", "text": "hunter2"},
        {"do": "key", "key": "Enter"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and len(body["results"]) == 5
    assert [x["do"] for x in body["results"]] == ["click", "type", "click",
                                                  "type", "key"]


def test_a_disabled_agent_501s_and_hides_it(locked):
    r = locked.post("/v1/input", headers=H,
                    json={"actions": [{"do": "click", "x": 1, "y": 1}]})
    assert r.status_code == 501 and "enabled = true" in r.json()["detail"]
    assert "input" not in locked.get("/v1/status", headers=H).json()["supports"]
    # inspect is a read, so it is not behind the same switch.
    assert "inspect" in locked.get("/v1/status", headers=H).json()["supports"]


def test_input_needs_the_token(client):
    assert client.post("/v1/input",
                       json={"actions": [{"do": "click", "x": 1, "y": 1}]}
                       ).status_code == 401


def test_a_bad_action_is_a_422(client):
    r = client.post("/v1/input", headers=H,
                    json={"actions": [{"do": "teleport"}]})
    assert r.status_code == 422


def test_too_many_actions_is_a_422(client):
    r = client.post("/v1/input", headers=H, json={
        "actions": [{"do": "wait", "ms": 1}] * 41})
    assert r.status_code == 422 and "at most 40" in r.json()["detail"]


def test_a_partial_run_is_a_200_that_says_so(client, cdp):
    """Not a 503: some of it happened, and the caller has to be able to see
    which. A status code cannot carry that."""
    cdp.found = None
    r = client.post("/v1/input", headers=H, json={"actions": [
        {"do": "wait", "ms": 1}, {"do": "click", "selector": "#gone"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert [x["ok"] for x in body["results"]] == [True, False]


def test_acting_on_a_screen_wakes_it(client, monkeypatch):
    """Unlike screenshot and inspect. Typing into a display is activity, and you
    want to see the result of it."""
    woken = []
    monkeypatch.setattr(appmod.display, "touch",
                        lambda s, url=None: woken.append(s["name"]))
    client.post("/v1/input", headers=H,
                json={"actions": [{"do": "click", "x": 1, "y": 1}]})
    assert woken == ["left"]


def test_looking_does_not_wake_anything(client, monkeypatch):
    woken = []
    monkeypatch.setattr(appmod.display, "touch",
                        lambda s, url=None: woken.append(s["name"]))
    client.get("/v1/inspect", headers=H)
    assert woken == []


def test_a_caller_may_ask_for_less_time_never_more(client, monkeypatch):
    """The deadline is what stops one request holding a threadpool thread while
    a page never settles, so the caller does not get to raise it."""
    import time
    seen = {}

    def spy(cfg, screen, actions, deadline=None):
        seen["deadline"] = deadline
        return []

    monkeypatch.setattr(appmod.browser, "input", spy)
    before = time.monotonic()
    client.post("/v1/input", headers=H, json={
        "deadline_ms": 900_000, "actions": [{"do": "wait", "ms": 1}]})
    # Clamped to the configured 30s, not the 15 minutes that was asked for.
    assert seen["deadline"] - before <= 31

    client.post("/v1/input", headers=H, json={
        "deadline_ms": 2_000, "actions": [{"do": "wait", "ms": 1}]})
    assert seen["deadline"] - time.monotonic() <= 2.1      # less is honoured


# --- the audit line ---------------------------------------------------------

def test_what_was_typed_never_reaches_the_log(client, caplog):
    """A password is what this route will mostly type. The log records that
    text was entered and how much, never what."""
    with caplog.at_level(logging.INFO, logger="room"):
        client.post("/v1/input", headers=H, json={"actions": [
            {"do": "click", "selector": "#pass"},
            {"do": "type", "text": "hunter2"}]})
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "hunter2" not in logged
    assert "type(7 chars)" in logged and "click" in logged


def test_a_stopped_sequence_is_logged_as_a_warning(client, cdp, caplog):
    cdp.found = None
    with caplog.at_level(logging.INFO, logger="room"):
        client.post("/v1/input", headers=H, json={
            "actions": [{"do": "click", "selector": "#gone"}]})
    assert any(r.levelno == logging.WARNING and "stopped at action" in r.getMessage()
               for r in caplog.records)


# --- the client and the CLI -------------------------------------------------

def test_the_cli_clicks_a_selector_or_a_point(client, monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(roomctl, "input",
                        lambda actions, *a, **k: sent.append(actions) or {"ok": True})
    assert cli.main(["click", "#login"]) == 0
    assert cli.main(["click", "812", "442"]) == 0
    assert cli.main(["click", "--right", "#x"]) == 0
    assert sent == [[{"do": "click", "selector": "#login"}],
                    [{"do": "click", "x": 812, "y": 442}],
                    [{"do": "right", "selector": "#x"}]]
    capsys.readouterr()


def test_the_cli_splits_a_key_combo(client, monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(roomctl, "input",
                        lambda actions, *a, **k: sent.append(actions) or {"ok": True})
    assert cli.main(["key", "ctrl+shift+a"]) == 0
    assert sent == [[{"do": "key", "key": "a", "modifiers": ["ctrl", "shift"]}]]
    capsys.readouterr()


def test_the_cli_refuses_a_click_it_cannot_parse(capsys):
    assert cli.main(["click", "1", "2", "3"]) == 1
    assert "selector, or two numbers" in capsys.readouterr().err
