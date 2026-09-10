"""Run: pytest.

The screens editor (PLAN.md §7, v1.1.0). What matters here is that a bad edit
never reaches disk — the Pi has no keyboard, so a bad save is not something you
can undo at the box — that a rename carries through to the idle page's
?screen=, and that the monitor layout is always xrandr's rather than a saved
copy of it that goes stale.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from agent import app as appmod
from agent import browser, display, settings
from agent.app import app

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
HOME = "http://pi:8080/home"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the agent at a throwaway settings.json."""
    p = tmp_path / "settings.json"
    monkeypatch.setenv("CROSSDROP_SETTINGS", str(p))
    return p


@pytest.fixture
def client(tmp_path, store, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'token = "{TOKEN}"\nhome_url = "{HOME}"\n'
        '[browser]\nkind = "chromium"\nautolaunch = false\n'
        '[[screen]]\nname = "left"\nposition = "0,0"\nsize = "800x600"\n'
        '[[screen]]\nname = "right"\nposition = "800,0"\nsize = "800x600"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CROSSDROP_CONFIG", str(cfg))
    # No browser behind this: place() is expected to fail and land in `note`.
    with TestClient(app) as c:
        yield c


# --- the file ---------------------------------------------------------------

def test_round_trip(store):
    settings.save({"screens": [{"name": "a"}]})
    assert settings.load() == {"screens": [{"name": "a"}]}


def test_missing_file_is_not_an_error(store):
    assert settings.load() == {}


def test_corrupt_file_falls_back_to_config(store):
    """A bad settings.json must cost you your overrides, not the agent."""
    store.write_text("{not json", encoding="utf-8")
    assert settings.load() == {}


def test_save_is_atomic(store):
    """No .tmp left behind, so a crashed write can't be read as settings."""
    settings.save({"screens": []})
    assert [p.name for p in store.parent.iterdir()] == [store.name]


# --- apply ------------------------------------------------------------------

def base_cfg():
    return {"screens": [{"name": "left", "home_url": HOME, "position": "0,0",
                         "size": "800x600"},
                        {"name": "right", "home_url": HOME, "position": "800,0",
                         "size": "800x600"}]}


def test_apply_matches_by_index_not_name(store):
    """The name is editable, so it cannot also be the key: matching on it would
    make every rename look like a new screen and silently drop the override."""
    cfg = base_cfg()
    settings.apply(cfg, {"screens": [{"name": "Samsung"}, {"name": "Acer"}]})
    assert [s["name"] for s in cfg["screens"]] == ["Samsung", "Acer"]


def test_a_saved_layout_never_beats_the_detected_one(store):
    """The whole point of the dynamic layout: settings.json may rename a screen
    and repoint its home_url, but geometry always comes from what is attached
    right now. A stale saved position used to put both windows on one monitor."""
    cfg = base_cfg()
    settings.apply(cfg, {"screens": [
        {"name": "x", "position": "9999,9999", "size": "1x1"}]})
    assert cfg["screens"][0]["name"] == "x", "the rename still applies"
    assert cfg["screens"][0]["position"] == "0,0"
    assert cfg["screens"][0]["size"] == "800x600"


def test_extra_saved_screens_are_ignored(store):
    """A monitor unplugged since the last save must not add a phantom screen."""
    cfg = base_cfg()
    settings.apply(cfg, {"screens": [{"name": "a"}, {"name": "b"}, {"name": "c"}]})
    assert len(cfg["screens"]) == 2


# --- merge ------------------------------------------------------------------

def test_a_save_keeps_screens_the_editor_could_not_see(store):
    """save() rewrites the file whole, and the editor only ever sees the
    monitors detected *now*. Unplug one, restart, save — and the other screen's
    name and home_url used to be gone for good."""
    settings.save({"screens": [{"name": "left", "home_url": "http://a/"},
                               {"name": "right", "home_url": "http://b/"}]})
    merged = settings.merge_screens([{"name": "renamed", "home_url": "http://c/"}])
    assert merged["screens"] == [{"name": "renamed", "home_url": "http://c/"},
                                 {"name": "right", "home_url": "http://b/"}]


def test_a_longer_edit_still_wins(store):
    """Plugging a monitor back in is the other direction: the new row is kept,
    not clipped to what happened to be on disk."""
    settings.save({"screens": [{"name": "only"}]})
    merged = settings.merge_screens([{"name": "a"}, {"name": "b"}])
    assert [s["name"] for s in merged["screens"]] == ["a", "b"]


# --- the routes -------------------------------------------------------------

def put(client, screens):
    return client.put("/v1/settings", json={"screens": screens}, headers=AUTH)


def ok_screens(**over):
    s = [{"name": "left", "home_url": HOME}, {"name": "right", "home_url": HOME}]
    s[1].update(over)
    return s


def test_auth_required(client):
    assert client.get("/v1/settings").status_code == 401


def test_get_reports_current_screens(client):
    body = client.get("/v1/settings", headers=AUTH).json()
    assert [s["name"] for s in body["screens"]] == ["left", "right"]
    assert body["path"].endswith("settings.json")


@pytest.mark.parametrize("screens, why", [
    ([{"name": "only", "home_url": HOME}], "wrong count"),
    (ok_screens(name="   "), "empty name"),
    (ok_screens(name="left"), "duplicate name"),
    (ok_screens(home_url="file:///etc/passwd"), "bad scheme"),
])
def test_bad_edits_are_rejected_and_nothing_is_written(client, store, screens, why):
    assert put(client, screens).status_code == 422, why
    assert not store.exists(), f"{why} reached disk"


def test_save_then_read_back(client, store):
    r = put(client, ok_screens(name="Acer"))
    assert r.status_code == 200
    assert [s["name"] for s in r.json()["screens"]] == ["left", "Acer"]
    assert json.loads(store.read_text())["screens"][1]["name"] == "Acer"


def test_rename_restamps_the_home_url(client):
    """The idle page names its monitor from ?screen=, so a rename has to move
    it — not append a second one after the old value."""
    put(client, ok_screens(name="Acer"))
    url = app.state.cfg["screens"][1]["home_url"]
    assert url == f"{HOME}?screen=Acer"


def test_config_dict_identity_survives_a_save(client):
    """display.watch() closed over this dict at startup. Reassigning it would
    leave the idle watcher reading a stale config forever."""
    before = app.state.cfg
    put(client, ok_screens(name="Acer"))
    assert app.state.cfg is before


def test_a_dead_browser_does_not_fail_the_save(tmp_path, store, monkeypatch):
    """The settings are already on disk by then; a window that could not be
    moved is a note, not a 500.

    Reaching place() at all now means the layout moved under the agent, which is
    exactly the case the windows have to follow -- no [[screen]] blocks, so the
    geometry comes from detect() and can differ between the request's two loads.
    """
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'token = "{TOKEN}"\nhome_url = "{HOME}"\n'
                   '[browser]\nkind = "chromium"\nautolaunch = false\n',
                   encoding="utf-8")
    monkeypatch.setenv("CROSSDROP_CONFIG", str(cfg))
    calls = []

    def detect():                   # the monitor moves between the two loads
        calls.append(1)
        return [{"output": "HDMI-1", "size": "800x600",
                 "position": "1920,0" if len(calls) > 1 else "0,0"}]

    monkeypatch.setattr(appmod.display, "detect", detect)
    with TestClient(app) as c:
        r = c.put("/v1/settings", headers=AUTH,
                  json={"screens": [{"name": "only", "home_url": HOME}]})
    assert r.status_code == 200, r.text
    assert "not moved" in r.json()["note"], r.text


def test_geometry_is_never_written_to_disk(client, store):
    """settings.json holds intent -- a name and a home_url. Hardware facts stay
    with the hardware, or the next monitor swap is fought by the last one."""
    assert put(client, ok_screens(name="Acer")).status_code == 200
    saved = json.loads(store.read_text())["screens"]
    assert all("position" not in s and "size" not in s for s in saved), saved


def test_an_unplugged_monitor_does_not_delete_its_saved_screen(tmp_path, store,
                                                               monkeypatch):
    """The whole path, not just the merge: two screens saved, one monitor
    unplugged, one edit made through a UI that can now only show one screen."""
    settings.save({"screens": [
        {"name": "left", "home_url": HOME, "position": "", "size": ""},
        {"name": "right", "home_url": "http://other/", "position": "", "size": ""}]})
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'token = "{TOKEN}"\nhome_url = "{HOME}"\n'
                   '[browser]\nkind = "chromium"\nautolaunch = false\n'
                   '[[screen]]\nname = "left"\n', encoding="utf-8")
    monkeypatch.setenv("CROSSDROP_CONFIG", str(cfg))
    with TestClient(app) as c:
        r = c.put("/v1/settings", headers=AUTH,
                  json={"screens": [{"name": "Samsung", "home_url": HOME}]})
        assert r.status_code == 200, r.text
    saved = json.loads(store.read_text())["screens"]
    assert [s["name"] for s in saved] == ["Samsung", "right"]
    assert saved[1]["home_url"] == "http://other/", "the unplugged screen was dropped"


def test_settings_survive_a_reload(client, store):
    """The v1.1.0 acceptance: an edit has to come back after a restart."""
    put(client, ok_screens(name="Acer"))
    assert appmod.load_config()["screens"][1]["name"] == "Acer"


def _last(tmp_path, url, age_s):
    settings.save({"screens": {"left": {"url": url, "at": time.time() - age_s}}},
                  settings.last_path())
    return {"display": display.DEFAULTS | {"restore_within_minutes": 60},
            "upload": {"dir": str(tmp_path)}}


def test_restore_only_what_is_recent(tmp_path):
    """The nightly restart puts yesterday evening's page back; it must not
    resurrect last week's."""
    cfg = _last(tmp_path, "http://x/chart", 30 * 60)
    assert appmod._restorable(cfg) == {"left": "http://x/chart"}

    cfg = _last(tmp_path, "http://x/chart", 8 * 3600)
    assert appmod._restorable(cfg) == {}

    cfg = _last(tmp_path, "http://x/chart", 30 * 60)
    cfg["display"]["restore_within_minutes"] = 0        # opted out entirely
    assert appmod._restorable(cfg) == {}


def test_restore_skips_an_upload_that_is_gone(tmp_path):
    """Uploads are tmpfs: after a reboot the id in last.json is a 404."""
    cfg = _last(tmp_path, "http://pi:8080/files/abcdefghijkl.pdf", 60)
    assert appmod._restorable(cfg) == {}
    (tmp_path / "abcdefghijkl.pdf").write_bytes(b"%PDF")
    assert appmod._restorable(cfg) != {}


def test_pair_still_backs_the_validation():
    """Guards the reuse: if browser._pair stops raising RuntimeError, the route
    silently accepts garbage instead of 422-ing."""
    with pytest.raises(RuntimeError):
        browser._pair("1366", ",", "position")


# --- the boot brick ---------------------------------------------------------
# settings.json is written by the agent and survives update.sh, the nightly
# restart and a reboot. Anything in it that stops load_config is therefore
# permanent, on a box with no keyboard. Two halves: the save is refused, and if
# one ever gets in anyway, the boot survives it.

@pytest.fixture
def one_screen(tmp_path, store, monkeypatch):
    """A live config with a single screen, the state a Pi is in while the second
    monitor is unplugged. Explicit, so it does not depend on what this dev box
    has attached."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'token = "{TOKEN}"\nhome_url = "{HOME}"\n'
                   '[browser]\nkind = "chromium"\nautolaunch = false\n'
                   '[[screen]]\nname = "left"\nposition = "0,0"\n',
                   encoding="utf-8")
    monkeypatch.setenv("CROSSDROP_CONFIG", str(cfg))
    return cfg


def test_a_name_already_taken_by_an_unplugged_screen_is_refused(one_screen, store):
    """The reachable brick, in the order it actually happens.

    Two monitors, saved as `left` and `right`. Unplug the second: detect() finds
    one, so the editor shows one row and merge_screens preserves `right` as an
    invisible tail. Rename the visible row to `right` -- both rows are now called
    `right`, which put_settings could not see because it only validated what the
    editor submitted. Plug the monitor back in and the two rows finally apply
    together, and every boot from then on raised.
    """
    settings.save({"screens": [
        {"name": "left", "home_url": HOME, "position": "0,0", "size": ""},
        {"name": "right", "home_url": HOME, "position": "1366,0", "size": ""}]})
    with TestClient(app) as c:
        r = c.put("/v1/settings", headers=AUTH, json={"screens": [
            {"name": "right", "home_url": HOME, "position": "0,0", "size": ""}]})
    assert r.status_code == 422, r.text
    assert "cannot see" in r.json()["detail"], r.text
    # And the collision never reached disk, so the next boot is unaffected.
    saved = [s["name"] for s in json.loads(store.read_text())["screens"]]
    assert saved == ["left", "right"], saved


def test_a_duplicate_that_got_in_anyway_does_not_stop_the_boot(tmp_path,
                                                              monkeypatch):
    """Defence in depth for the above. A saved name must never be able to
    prevent a boot: there is no keyboard, and the file outlives every restart.
    Renaming the loser is recoverable -- the screen picker shows `right-2`,
    which is odd enough to notice and fixable from the same UI."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('token = "t"\nhome_url = "https://e.test/"\n'
                        '[[screen]]\nname = "left"\nposition = "0,0"\n'
                        '[[screen]]\nname = "right"\nposition = "1366,0"\n',
                        encoding="utf-8")
    monkeypatch.setenv("CROSSDROP_CONFIG", str(cfg_path))
    settings.save({"screens": [
        {"name": "right", "home_url": "https://e.test/", "position": "0,0"},
        {"name": "right", "home_url": "https://e.test/", "position": "1366,0"}]})
    cfg = appmod.load_config()                  # used to raise RuntimeError
    assert [s["name"] for s in cfg["screens"]] == ["right", "right-2"]


def test_a_duplicate_written_by_hand_is_still_refused(tmp_path, monkeypatch):
    """The opposite answer, deliberately. config.toml is edited by somebody who
    had a keyboard when they wrote it, so a typo there should be named rather
    than silently renamed."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('token = "t"\nhome_url = "https://e.test/"\n'
                        '[[screen]]\nname = "same"\n[[screen]]\nname = "same"\n',
                        encoding="utf-8")
    monkeypatch.setenv("CROSSDROP_CONFIG", str(cfg_path))
    with pytest.raises(RuntimeError, match="duplicate"):
        appmod.load_config()


def test_a_monitor_appearing_mid_save_is_not_a_500(tmp_path, store, monkeypatch):
    """`before` was read from the old config and indexed with the new one's
    enumeration, so a monitor plugged in between the length check and the reload
    raised IndexError -- a 500 out of a route that had already written the file,
    leaving the caller unable to tell whether the save landed.

    No [[screen]] blocks, so the screen list comes from display.detect() and can
    genuinely change length between the two load_config calls in one request."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'token = "{TOKEN}"\nhome_url = "{HOME}"\n'
                   '[browser]\nkind = "chromium"\nautolaunch = false\n',
                   encoding="utf-8")
    monkeypatch.setenv("CROSSDROP_CONFIG", str(cfg))
    calls = []

    def detect():
        calls.append(1)
        # Call 1 is the lifespan's load_config; call 2 is the reload inside the
        # request, which is where the second monitor has to appear.
        return [{"output": "HDMI-1", "position": "0,0", "size": "800x600"}] + (
            [{"output": "HDMI-2", "position": "800,0", "size": "800x600"}]
            if len(calls) > 1 else [])

    monkeypatch.setattr(appmod.display, "detect", detect)
    with TestClient(app) as c:
        r = c.put("/v1/settings", headers=AUTH, json={"screens": [
            {"name": "only", "home_url": HOME, "position": "0,0",
             "size": "800x600"}]})
    assert r.status_code == 200, r.text


def test_a_generated_name_cannot_collide_with_a_written_one(tmp_path, monkeypatch):
    """The duplicate check ran before the default names were filled in, so it
    only saw what was written. An unnamed first block beside `name = "main"`
    produced two screens both called `main` -- no error, and the second monitor
    unaddressable by any name, with browser._targets mapping both to one CDP
    target and /v1/screens listing two identical rows."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('token = "t"\nhome_url = "https://e.test/"\n'
                        '[[screen]]\nposition = "0,0"\n'
                        '[[screen]]\nname = "main"\nposition = "1366,0"\n',
                        encoding="utf-8")
    monkeypatch.setenv("CROSSDROP_CONFIG", str(cfg_path))
    with pytest.raises(RuntimeError, match="duplicate"):
        appmod.load_config()


def test_a_generated_name_collides_with_screen2_too(tmp_path, monkeypatch):
    """The other direction: `screen2` is the generated name for the second
    block, so writing it on the first one collides the same way."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('token = "t"\nhome_url = "https://e.test/"\n'
                        '[[screen]]\nname = "screen2"\nposition = "0,0"\n'
                        '[[screen]]\nposition = "1366,0"\n', encoding="utf-8")
    monkeypatch.setenv("CROSSDROP_CONFIG", str(cfg_path))
    with pytest.raises(RuntimeError, match="duplicate"):
        appmod.load_config()


@pytest.mark.parametrize("body", [
    '{"screens": {"a": 1}}',
    '{"screens": ["x"]}',
    '{"screens": [null]}',
    '{"screens": 5}',
    '{"screens": [{"name": "ok"}, 7]}',
])
def test_a_wrong_shaped_settings_file_does_not_stop_the_boot(tmp_path, monkeypatch,
                                                             store, body):
    """load() only ever proved the top level is a dict, so every one of these
    reached apply()'s loop and raised AttributeError or TypeError out of
    load_config. lifespan does not catch that, so uvicorn died and
    Restart=always looped it -- and this file outlives update.sh and the nightly
    restart, so the crash loop was permanent on a box with no keyboard.

    Exactly the property _dedupe's docstring argues must never exist, guarded
    for names and unguarded for shape. The overrides are what you lose."""
    store.write_text(body, encoding="utf-8")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('token = "t"\nhome_url = "https://e.test/"\n'
                        '[[screen]]\nname = "left"\n', encoding="utf-8")
    monkeypatch.setenv("CROSSDROP_CONFIG", str(cfg_path))
    cfg = appmod.load_config()          # used to raise, and take uvicorn with it
    # One screen, usably named. A *valid* row in the list is still allowed to
    # override, so the name may be the saved one -- what matters is that a bad
    # row costs the overrides rather than the boot.
    assert len(cfg["screens"]) == 1
    assert isinstance(cfg["screens"][0]["name"], str) and cfg["screens"][0]["name"]


def test_a_wrong_shaped_settings_file_does_not_break_a_save(store, one_screen):
    """merge_screens indexes the saved list too, so the same shapes came back
    out of PUT /v1/settings as a 500 -- and a non-object row would have been
    written straight back for the next boot to choke on."""
    store.write_text('{"screens": ["not a screen", {"name": "keep"}]}',
                     encoding="utf-8")
    with TestClient(app) as c:
        r = c.put("/v1/settings", headers=AUTH, json={"screens": [
            {"name": "left", "home_url": HOME, "position": "", "size": ""}]})
    assert r.status_code == 200, r.text
    saved = json.loads(store.read_text())["screens"]
    assert all(isinstance(row, dict) for row in saved), saved


# --- splitting a panel, and the identity that survives it --------------------

def split_cfg(tmp_path, monkeypatch, mode="fullscreen", splits=None, saved=None):
    """A live load_config with two stub monitors and a settings.json."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'token = "{TOKEN}"\nhome_url = "{HOME}"\n'
                   '[browser]\nkind = "chromium"\nautolaunch = false\n',
                   encoding="utf-8")
    monkeypatch.setenv("CROSSDROP_CONFIG", str(cfg))
    monkeypatch.setattr(appmod.display, "detect", lambda: [
        {"output": "HDMI-1", "position": "0,0", "size": "2560x1440"},
        {"output": "HDMI-2", "position": "2560,0", "size": "1366x768"}])
    data = {"browser": {"mode": mode}, **(saved or {})}
    if splits is not None:
        data["display"] = {"splits": splits}
    settings.save(data)
    return appmod.load_config()


def test_a_split_makes_two_screens_out_of_one_panel(tmp_path, monkeypatch, store):
    cfg = split_cfg(tmp_path, monkeypatch, splits={"HDMI-1": "lr"})
    assert [s["output"] for s in cfg["screens"]] == ["HDMI-1-L", "HDMI-1-R", "HDMI-2"]
    assert [s["position"] for s in cfg["screens"]] == ["0,0", "1280,0", "2560,0"]
    assert [s["fullscreen"] for s in cfg["screens"]] == [False, False, True]


def test_a_split_is_refused_in_kiosk_mode(tmp_path, monkeypatch, store, caplog):
    """A --kiosk window may not hold half-screen bounds, and two halves that
    both come up fullscreen sit on top of each other -- which _by_bounds then
    has to refuse, taking /v1/input with it. Don't offer the combination."""
    with caplog.at_level("WARNING"):
        cfg = split_cfg(tmp_path, monkeypatch, mode="kiosk",
                        splits={"HDMI-1": "lr"})
    assert [s["output"] for s in cfg["screens"]] == ["HDMI-1", "HDMI-2"]
    assert "staying whole" in caplog.text, caplog.text


def test_a_name_follows_its_panel_across_a_split(tmp_path, monkeypatch, store):
    """The reason rows are keyed by output. Splitting HDMI-1 inserts a row, so
    under index-as-identity the name saved for HDMI-2 would slide onto the new
    right half -- silently renaming two screens at once."""
    saved = {"screens": [{"output": "HDMI-2", "name": "Acer",
                          "home_url": "https://acer.test/"}]}
    cfg = split_cfg(tmp_path, monkeypatch, splits={"HDMI-1": "lr"}, saved=saved)
    by_out = {s["output"]: s["name"] for s in cfg["screens"]}
    assert by_out["HDMI-2"] == "Acer"
    assert by_out["HDMI-1-L"] == "HDMI-1-L", by_out
    assert by_out["HDMI-1-R"] == "HDMI-1-R", by_out


def test_a_row_saved_before_output_existed_still_applies(tmp_path, monkeypatch,
                                                         store):
    """The whole migration: an old settings.json is a list with no `output`, and
    must keep meaning what it meant -- position in the list."""
    saved = {"screens": [{"name": "Samsung"}, {"name": "Acer"}]}
    cfg = split_cfg(tmp_path, monkeypatch, saved=saved)
    assert [s["name"] for s in cfg["screens"]] == ["Samsung", "Acer"]


def test_a_split_saved_through_the_route_takes_effect(tmp_path, monkeypatch, store):
    """The UI's whole path: PUT the split, and the next load has four screens."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'token = "{TOKEN}"\nhome_url = "{HOME}"\n'
                   '[browser]\nkind = "chromium"\nautolaunch = false\n'
                   'mode = "fullscreen"\n', encoding="utf-8")
    monkeypatch.setenv("CROSSDROP_CONFIG", str(cfg))
    monkeypatch.setattr(appmod.display, "detect", lambda: [
        {"output": "HDMI-1", "position": "0,0", "size": "2560x1440"},
        {"output": "HDMI-2", "position": "2560,0", "size": "1366x768"}])
    with TestClient(app) as c:
        body = c.get("/v1/settings", headers=AUTH).json()
        assert body["outputs"] == ["HDMI-1", "HDMI-2"], body
        assert body["mode"] == "fullscreen"
        r = c.put("/v1/settings", headers=AUTH, json={
            "screens": [{"name": s["name"], "home_url": s["home_url"]}
                        for s in body["screens"]],
            "splits": {"HDMI-1": "lr"}})
        assert r.status_code == 200, r.text
        assert [s["output"] for s in r.json()["screens"]] == [
            "HDMI-1-L", "HDMI-1-R", "HDMI-2"]
        assert [s["fullscreen"] for s in r.json()["screens"]] == [False, False, True]


def test_the_route_refuses_a_split_in_kiosk_mode(client, store, monkeypatch):
    """load_config would drop it silently on the next boot; refusing the save is
    where it can still be explained to whoever pressed the button.

    The monitor is real here, so the refusal has to be about the *mode* and not
    about an output that happens not to exist on the machine running the tests.
    """
    monkeypatch.setattr(appmod.display, "detect", lambda: [
        {"output": "HDMI-1", "position": "0,0", "size": "2560x1440"}])
    r = client.put("/v1/settings", headers=AUTH, json={
        "screens": ok_screens(), "splits": {"HDMI-1": "lr"}, "mode": "kiosk"})
    assert r.status_code == 422
    assert "fullscreen" in r.json()["detail"], r.text
    assert not store.exists(), "a refused split reached disk"


def test_the_route_refuses_a_split_of_a_monitor_that_is_not_there(client, store):
    """Silently doing nothing is the worst answer on a box you cannot look at."""
    r = client.put("/v1/settings", headers=AUTH, json={
        "screens": ok_screens(), "splits": {"HDMI-9": "lr"}, "mode": "fullscreen"})
    assert r.status_code == 422 and "no output" in r.json()["detail"]
    assert not store.exists()


def test_a_save_that_does_not_mention_splits_keeps_them(tmp_path, monkeypatch, store):
    """save() rewrites the file whole, so anything not put back is deleted --
    renaming a screen must not quietly un-split the wall."""
    split_cfg(tmp_path, monkeypatch, mode="fullscreen", splits={"HDMI-1": "lr"})
    with TestClient(app) as c:
        body = c.get("/v1/settings", headers=AUTH).json()
        r = c.put("/v1/settings", headers=AUTH, json={
            "screens": [{"name": s["name"], "home_url": s["home_url"]}
                        for s in body["screens"]]})
        assert r.status_code == 200, r.text
    assert json.loads(store.read_text())["display"]["splits"] == {"HDMI-1": "lr"}
