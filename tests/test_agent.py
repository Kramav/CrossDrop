"""Run: pytest. Real-browser smoke test: ROOM_SMOKE=1 pytest -s"""

import os

import pytest
from fastapi.testclient import TestClient

from agent.app import app

TOKEN = "test-token"


def write_config(tmp_path, autolaunch=False, kind=None):
    p = tmp_path / "config.toml"
    p.write_text(
        f'token = "{TOKEN}"\nhome_url = "about:blank"\n'
        f'[browser]\nkind = "{kind or os.getenv("ROOM_BROWSER", "firefox")}"\n'
        f"autolaunch = {str(autolaunch).lower()}\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOM_CONFIG", str(write_config(tmp_path)))
    with TestClient(app) as c:
        yield c


def test_auth_required(client):
    assert client.post("/v1/navigate", json={"url": "https://example.com"}).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.post("/v1/navigate", json={"url": "https://example.com"}, headers=bad).status_code == 401


def test_scheme_allowlist(client):
    auth = {"Authorization": f"Bearer {TOKEN}"}
    for url in ("file:///etc/passwd", "javascript:alert(1)", "not a url"):
        assert client.post("/v1/navigate", json={"url": url}, headers=auth).status_code == 422


def test_status_reports_dead_browser(client):
    r = client.get("/v1/status", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json() == {"up": True, "current_url": None, "browser": "down", "version": "dev"}


@pytest.mark.skipif(not os.getenv("ROOM_SMOKE"), reason="set ROOM_SMOKE=1 to drive a real browser")
def test_smoke_navigate(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOM_CONFIG", str(write_config(tmp_path, autolaunch=True)))
    with TestClient(app) as c:
        auth = {"Authorization": f"Bearer {TOKEN}"}
        r = c.post("/v1/navigate", json={"url": "https://example.com"}, headers=auth)
        assert r.status_code == 200, r.text
        s = c.get("/v1/status", headers=auth).json()
        assert s["browser"] == "ok", s
        assert "example.com" in s["current_url"], s
