import pytest


@pytest.fixture(autouse=True)
def isolate_settings(tmp_path, monkeypatch):
    """Never read the developer's real settings.json.

    load_config() overlays it onto every config it builds, so without this a
    saved screen rename on this machine would quietly change what half the
    suite asserts. test_settings.py overrides ROOM_SETTINGS again with its own
    path; setting it twice is harmless.
    """
    monkeypatch.setenv("ROOM_SETTINGS", str(tmp_path / "no-settings.json"))
