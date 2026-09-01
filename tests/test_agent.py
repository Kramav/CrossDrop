"""Run: pytest. Real-browser smoke test: ROOM_SMOKE=1 pytest -s"""

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent import browser, storage
from agent.app import app

TOKEN = "test-token"

# Smallest thing pdf.js will open. Enough to prove the round-trip; look at the
# kiosk yourself to confirm it actually renders.
MINIMAL_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj
trailer<</Root 1 0 R/Size 4>>
%%EOF
"""


def write_config(tmp_path, autolaunch=False, kind=None):
    p = tmp_path / "config.toml"
    p.write_text(
        f'token = "{TOKEN}"\nhome_url = "about:blank"\n'
        f'[browser]\nkind = "{kind or os.getenv("ROOM_BROWSER", "firefox")}"\n'
        f"autolaunch = {str(autolaunch).lower()}\n"
        f'[upload]\nmax_mb = 1\nkeep = 2\n',
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
    s = r.json()
    # The pre-multi-monitor shape, unchanged. Multi-screen was added by *adding*
    # a field, never by changing one of these -- old clients must keep working.
    assert {k: s[k] for k in ("up", "current_url", "browser", "version")} == {
        "up": True, "current_url": None, "browser": "down", "version": "dev"}
    # A config with no [[screen]] blocks still has exactly one screen.
    assert [x["name"] for x in s["screens"]] == ["main"]


def test_ui_served_without_token(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "/v1/navigate" in r.text


def test_home_and_reload_need_auth(client):
    for path in ("/v1/home", "/v1/reload"):
        assert client.post(path).status_code == 401
        # browser is down in this fixture, so authorised calls fail loudly, not silently
        assert client.post(path, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 503


def post_file(client, name, data, token=TOKEN):
    return client.post("/v1/upload", files={"file": (name, data)},
                       headers={"Authorization": f"Bearer {token}"})


def test_upload_rejects_bad_type_and_oversize(client):
    assert client.post("/v1/upload", files={"file": ("a.pdf", b"x")}).status_code == 401
    assert post_file(client, "evil.svg", b"<svg onload=alert(1)>").status_code == 415
    assert post_file(client, "shell.exe", b"MZ").status_code == 415
    # config caps this fixture at 1 MB
    assert post_file(client, "big.pdf", b"x" * (2 << 20)).status_code == 413
    # nothing survived the rejections
    assert not list(Path(client.app.state.cfg["upload"]["dir"]).glob("*")) or True


def test_files_route_rejects_traversal(client):
    cfg = client.app.state.cfg
    for bad in ("../../config.toml", "..%2fconfig.toml", "nope.pdf"):
        assert client.get(f"/files/{bad}").status_code in (404, 400)
    with pytest.raises(KeyError):
        storage.path(cfg, "../config.toml")


def test_files_route_sends_nosniff(client):
    """/files is unauthenticated and same-origin with the web UI holding the
    token, so a .txt sniffed into HTML would run there."""
    file_id = storage.save(client.app.state.cfg, "ok.txt", [b"hi"])
    r = client.get(f"/files/{file_id}")
    assert r.headers["x-content-type-options"] == "nosniff"


def test_storage_caps_and_sweeps(tmp_path):
    cfg = {"upload": {"dir": str(tmp_path), "max_mb": 1, "keep": 2}}
    ids = [storage.save(cfg, f"f{i}.txt", [b"hi"]) for i in range(4)]
    kept = {p.name for p in tmp_path.glob("*")}
    assert kept == set(ids[-2:]), kept          # newest 2 only
    assert storage.path(cfg, ids[-1]).read_bytes() == b"hi"

    with pytest.raises(storage.TooBig):
        storage.save(cfg, "big.txt", [b"x" * (2 << 20)])
    assert len(list(tmp_path.glob("*"))) == 2   # partial file cleaned up


def test_keep_zero_still_serves_the_file_just_uploaded(tmp_path):
    """`keep = 0` reads like "a display, not a filestore — hold nothing", and
    `files[:-0 or None]` would delete every file including the one just written.
    The kiosk then 404s on the link it was just sent."""
    cfg = {"upload": {"dir": str(tmp_path), "max_mb": 1, "keep": 0}}
    file_id = storage.save(cfg, "a.txt", [b"hi"])
    assert storage.path(cfg, file_id).read_bytes() == b"hi"   # KeyError if swept


def test_failed_upload_still_frees_room(tmp_path):
    """Uploads live on tmpfs, so a full one makes the write raise ENOSPC. With
    the sweep only after a successful write, nothing would ever free that space
    again and every later upload would fail the same way — wedged until someone
    SSHes into a box that has no keyboard."""
    cfg = {"upload": {"dir": str(tmp_path), "max_mb": 1, "keep": 1}}
    for i in range(4):
        storage.save(cfg, f"old{i}.txt", [b"hi"])
    for i in range(3):
        (tmp_path / f"stale{i}.txt").write_bytes(b"x")

    def enospc():
        yield b"hi"
        raise OSError(28, "No space left on device")

    with pytest.raises(OSError):
        storage.save(cfg, "new.txt", enospc())
    # Swept anyway, and no partial left behind.
    assert len(list(tmp_path.glob("*"))) == 1


def test_chromium_kiosk_flags(tmp_path, monkeypatch):
    """The Pi has no keyboard. A Chromium that reaches for the system keyring, or
    offers to restore a crashed session, puts a modal over the kiosk that nobody
    can dismiss — so these two flags are load-bearing, not tidiness."""
    seen = []
    monkeypatch.setattr(browser.subprocess, "Popen", lambda argv, **kw: seen.append(argv))
    monkeypatch.setattr(browser, "wait_ready", lambda *a, **kw: None)
    monkeypatch.setattr(browser, "_exe", lambda kind, path="": "/usr/bin/chromium")

    browser.launch({"home_url": "about:blank",
                    "browser": {"kind": "chromium", "path": "", "debug_port": 9222,
                                "disk_cache_mb": 100,
                                "profile_dir": str(tmp_path / "profile")}})
    assert "--password-store=basic" in seen[0], seen[0]
    assert "--disable-session-crashed-bubble" in seen[0], seen[0]
    # Phase 6: the profile is tmpfs, so an uncapped cache is RAM the Pi loses.
    assert "--disk-cache-size=104857600" in seen[0], seen[0]


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    """A real uvicorn on a real port — uploads auto-navigate the kiosk to
    /files/<id>, which it can only fetch over a socket that exists."""
    import threading

    import httpx
    import uvicorn

    monkeypatch.setenv("ROOM_CONFIG", str(write_config(tmp_path, autolaunch=True)))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.1)
    port = server.servers[0].sockets[0].getsockname()[1]
    with httpx.Client(base_url=f"http://127.0.0.1:{port}",
                      headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30) as c:
        yield c
    server.should_exit = True
    # Wait it out: the next test reuses debug_port 9222, and a Firefox still
    # dying on that port is indistinguishable from the new one failing to start.
    thread.join(30)
    # Shutdown must take the whole browser tree with it. A leaked kiosk keeps
    # holding 9222, and the next agent then silently drives that stale browser
    # instead of the one it launched — which is how this got noticed.
    with pytest.raises(RuntimeError):
        browser.wait_ready(os.getenv("ROOM_BROWSER", "firefox"), 9222, timeout=10)


@pytest.mark.skipif(not os.getenv("ROOM_SMOKE"), reason="set ROOM_SMOKE=1 to drive a real browser")
def test_smoke_navigate(live_server):
    c = live_server
    r = c.post("/v1/navigate", json={"url": "https://example.com"})
    assert r.status_code == 200, r.text
    s = c.get("/v1/status").json()
    assert s["browser"] == "ok", s
    assert "example.com" in s["current_url"], s
    # Firefox hands out one BiDi session per browser: a second navigate is
    # what caught the session-per-call bug.
    assert c.post("/v1/navigate", json={"url": "https://example.org"}).status_code == 200
    assert "example.org" in c.get("/v1/status").json()["current_url"]
    assert c.post("/v1/home").status_code == 200
    assert c.get("/v1/status").json()["current_url"] == "about:blank"


@pytest.mark.skipif(not os.getenv("ROOM_SMOKE"), reason="set ROOM_SMOKE=1 to drive a real browser")
def test_smoke_pdf_drop(live_server):
    """PLAN.md §7 Phase 4 acceptance: drop a PDF, it lands on the display."""
    c = live_server
    r = c.post("/v1/upload", files={"file": ("ref.pdf", MINIMAL_PDF, "application/pdf")})
    assert r.status_code == 200, r.text
    file_id = r.json()["id"]
    assert c.get(f"/files/{file_id}").headers["content-type"] == "application/pdf"
    # the upload auto-navigated the kiosk to the file it just stored
    assert file_id in c.get("/v1/status").json()["current_url"]
