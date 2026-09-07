import pytest


@pytest.fixture
def cli_target(tmp_path, monkeypatch):
    """A targets.toml the CLI can resolve, for tests that stub Client's methods.

    roomctl.cli builds a real `roomctl.Client` now — the by-name wrapper layer
    that used to absorb the whole call is gone — so it needs a url and a token
    even when every method on it is patched out. Nothing is ever dialled.
    """
    p = tmp_path / "targets.toml"
    p.write_text('[t]\nurl = "http://127.0.0.1:1"\ntoken = "x"\n', encoding="utf-8")
    monkeypatch.setenv("ROOMCTL_TARGETS", str(p))
    return p


@pytest.fixture(autouse=True)
def isolate_settings(tmp_path, monkeypatch):
    """Never read the developer's real settings.json.

    load_config() overlays it onto every config it builds, so without this a
    saved screen rename on this machine would quietly change what half the
    suite asserts. test_settings.py overrides ROOM_SETTINGS again with its own
    path; setting it twice is harmless.
    """
    monkeypatch.setenv("ROOM_SETTINGS", str(tmp_path / "no-settings.json"))
