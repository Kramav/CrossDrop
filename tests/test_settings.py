"""Run: pytest.

The screens editor (PLAN.md §7, v1.1.0). What matters here is that a bad edit
never reaches disk — the Pi has no keyboard, so a saved position that breaks
placement is not something you can undo at the box — and that a rename carries
through to the idle page's ?screen=.
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
    monkeypatch.setenv("ROOM_SETTINGS", str(p))
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
    monkeypatch.setenv("ROOM_CONFIG", str(cfg))
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


def test_blank_override_falls_back_to_detected(store):
    """Blanking position in the UI is the re-detect path."""
    cfg = base_cfg()
    settings.apply(cfg, {"screens": [{"name": "x", "position": ""}]})
    assert cfg["screens"][0]["position"] == "0,0"


def test_extra_saved_screens_are_ignored(store):
    """A monitor unplugged since the last save must not add a phantom screen."""
    cfg = base_cfg()
    settings.apply(cfg, {"screens": [{"name": "a"}, {"name": "b"}, {"name": "c"}]})
    assert len(cfg["screens"]) == 2


# --- the routes -------------------------------------------------------------

def put(client, screens):
    return client.put("/v1/settings", json={"screens": screens}, headers=AUTH)


def ok_screens(**over):
    s = [{"name": "left", "home_url": HOME, "position": "0,0", "size": "800x600"},
         {"name": "right", "home_url": HOME, "position": "800,0", "size": "800x600"}]
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
    (ok_screens(position="1366"), "position missing a comma"),
    (ok_screens(size="2560*1440"), "size with the wrong separator"),
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


def test_a_dead_browser_does_not_fail_the_save(client, store):
    """The settings are already on disk by then; a window that could not be
    moved is a note, not a 500."""
    r = put(client, ok_screens(position="1920,0"))
    assert r.status_code == 200
    assert "not moved" in r.json()["note"]
    assert json.loads(store.read_text())["screens"][1]["position"] == "1920,0"


def test_blank_position_is_saved_as_a_reset(client, store):
    r = put(client, ok_screens(position="", size=""))
    assert r.status_code == 200
    assert json.loads(store.read_text())["screens"][1]["position"] == ""
    # Nothing to place, so no note about a window that was never asked to move.
    assert r.json()["note"] == ""


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
