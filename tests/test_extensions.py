"""Run: pytest.

Extension install, with the Web Store stubbed. Nothing here touches the network:
`install()` takes its opener as an argument precisely so this file can hand it a
CRX it built itself.
"""

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from agent import app as appmod
from agent import browser, extensions

AUTH = {"Authorization": "Bearer t"}


def crx(members=None, header=b"Cr24\x03\x00\x00\x00garbage-header-bytes"):
    """A CRX3: a header, then a zip. The header is why we can't just unzip it."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in (members or {
                "manifest.json": '{"name": "uBlock Test", "manifest_version": 3}',
                "sw.js": "// x",
        }).items():
            z.writestr(name, data)
    return header + buf.getvalue()


def opener(blob, calls=None):
    """Stand-in for urllib.request.urlopen."""
    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return blob[:n] if n and n > 0 else blob

    def _open(url, timeout=None):
        if calls is not None:
            calls.append(url)
        if isinstance(blob, Exception):
            raise blob
        return R()
    return _open


ID = "a" * 32
ID2 = "b" * 32


# --- unpacking --------------------------------------------------------------

def test_crx_unpacks_despite_its_header(tmp_path):
    """The assumption the whole approach rests on: zipfile reads an archive with
    junk in front of it, so no unzip binary and no subprocess."""
    name = extensions.install(str(tmp_path), ID, _open=opener(crx()))
    assert name == "uBlock Test"
    assert (tmp_path / ID / "manifest.json").is_file()
    assert extensions.scan(str(tmp_path)) == [str(tmp_path / ID)]


def test_reinstall_replaces_in_place(tmp_path):
    extensions.install(str(tmp_path), ID, _open=opener(crx()))
    extensions.install(str(tmp_path), ID, _open=opener(crx({
        "manifest.json": '{"name": "Renamed", "manifest_version": 3}'})))
    assert extensions.scan(str(tmp_path)) == [str(tmp_path / ID)]
    assert extensions.display_name(tmp_path / ID) == "Renamed"


def test_zip_slip_stays_inside(tmp_path):
    """A member named ../evil must not write outside the destination. CPython's
    extractall sanitises; this pins it, because the archive is remote code."""
    dest = tmp_path / "ext"
    blob = crx({"manifest.json": '{"name": "x"}', "../../evil.js": "pwn"})
    extensions.install(str(dest), ID, _open=opener(blob))
    assert not (tmp_path.parent / "evil.js").exists()
    assert not (tmp_path / "evil.js").exists()
    assert (dest / ID / "evil.js").is_file()


def test_oversized_download_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "MAX_MB", 0.001)     # 1 KB
    with pytest.raises(extensions.TooBig):
        extensions.install(str(tmp_path), ID, _open=opener(crx() + b"\0" * 2048))
    assert extensions.scan(str(tmp_path)) == []


def test_not_an_extension_leaves_nothing_behind(tmp_path):
    """The store serving an HTML error page must not become "installed"."""
    blob = crx({"readme.txt": "not an extension"})
    with pytest.raises(ValueError, match="manifest"):
        extensions.install(str(tmp_path), ID, _open=opener(blob))
    assert list(tmp_path.iterdir()) == []


def test_bad_id_never_reaches_the_network(tmp_path):
    calls = []
    for bad in ["", "zz", "A" * 32, "../etc/passwd", "q" * 32]:
        with pytest.raises(extensions.BadId):
            extensions.install(str(tmp_path), bad, _open=opener(crx(), calls))
    assert calls == []


# --- scan / name / pending --------------------------------------------------

def test_half_unpacked_extension_never_blocks_the_kiosk(tmp_path):
    """An interrupted unpack leaves a directory with no manifest. Passing that to
    Chromium fails the launch, and a display that will not come up is worse than
    a missing ad blocker."""
    extensions.install(str(tmp_path), ID, _open=opener(crx()))
    (tmp_path / "torn").mkdir()
    assert extensions.scan(str(tmp_path)) == [str(tmp_path / ID)]
    assert extensions.scan(str(tmp_path / "nope")) == []
    assert extensions.scan("") == []


def test_display_name_resolves_the_i18n_placeholder(tmp_path):
    """uBlock Origin Lite's manifest name is __MSG_extName__. Unresolved, the UI
    lists 32-character hashes."""
    blob = crx({"manifest.json": json.dumps(
                    {"name": "__MSG_extName__", "default_locale": "en"}),
                "_locales/en/messages.json": json.dumps(
                    {"extName": {"message": "uBlock Origin Lite"}})})
    assert extensions.install(str(tmp_path), ID, _open=opener(blob)) == "uBlock Origin Lite"


def test_display_name_falls_back_to_the_directory(tmp_path):
    """A missing or broken label must never fail a route that otherwise worked."""
    blob = crx({"manifest.json": '{"name": "__MSG_missing__", "default_locale": "en"}'})
    assert extensions.install(str(tmp_path), ID, _open=opener(blob)) == ID
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "manifest.json").write_text("{not json")
    assert extensions.display_name(tmp_path / "broken") == "broken"


def test_pending_tracks_disk_against_the_running_browser(tmp_path):
    extensions.install(str(tmp_path), ID, _open=opener(crx()))
    assert extensions.pending(str(tmp_path), []) is True
    loaded = extensions.scan(str(tmp_path))
    assert extensions.pending(str(tmp_path), loaded) is False
    extensions.remove(str(tmp_path), ID)                 # a removal is pending too
    assert extensions.pending(str(tmp_path), loaded) is True


def test_remove_resolves_by_name_not_by_path(tmp_path):
    extensions.install(str(tmp_path), ID, _open=opener(crx()))
    with pytest.raises(KeyError):
        extensions.remove(str(tmp_path), "../" + ID)
    assert extensions.scan(str(tmp_path))                # still there


# --- routes -----------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'''token = "t"
home_url = "about:blank"
[browser]
autolaunch = false
kind = "chromium"
extensions_dir = "{(tmp_path / 'ext').as_posix()}"
[upload]
max_mb = 1
''', encoding="utf-8")
    monkeypatch.setenv("ROOM_CONFIG", str(cfg))
    browser._loaded.clear()
    with TestClient(appmod.app) as c:
        yield c


def test_install_several_at_once(client, monkeypatch):
    monkeypatch.setattr(extensions, "install", lambda d, i, **kw: "Blocker")
    r = client.post("/v1/extensions", json={"ids": [ID, ID2]}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert [x["id"] for x in body["results"]] == [ID, ID2]


def test_one_bad_id_fails_the_whole_request(client):
    r = client.post("/v1/extensions", json={"ids": [ID, "nope"]}, headers=AUTH)
    assert r.status_code == 422
    assert "nope" in r.json()["detail"]


def test_partial_failure_is_reported_per_id(client, monkeypatch):
    def fake(d, i, **kw):
        if i == ID2:
            raise OSError("404 from the store")
        return "Blocker"

    monkeypatch.setattr(extensions, "install", fake)
    body = client.post("/v1/extensions", json={"ids": [ID, ID2]}, headers=AUTH).json()
    assert body["ok"] is False
    assert body["results"][0] == {"id": ID, "ok": True, "name": "Blocker", "error": None}
    assert body["results"][1]["ok"] is False
    assert "404" in body["results"][1]["error"]


def test_install_then_list_then_remove(client, monkeypatch):
    real = extensions.install          # bind before patching, or this recurses
    monkeypatch.setattr(extensions, "install",
                        lambda d, i, **kw: real(d, i, _open=opener(crx())))
    r = client.post("/v1/extensions", json={"ids": [ID]}, headers=AUTH)
    assert r.status_code == 200

    body = client.get("/v1/extensions", headers=AUTH).json()
    assert [x["id"] for x in body["installed"]] == [ID]
    # autolaunch = false, so nothing is loaded and everything installed is pending
    assert body["pending_restart"] is True

    body = client.request("DELETE", f"/v1/extensions/{ID}", headers=AUTH).json()
    assert body["installed"] == []
    assert client.request("DELETE", f"/v1/extensions/{ID}",
                          headers=AUTH).status_code == 404


def test_needs_a_token(client):
    assert client.get("/v1/extensions").status_code == 401


def test_cli_install_takes_several_ids(monkeypatch, capsys):
    import roomctl
    from roomctl import cli
    seen = {}
    monkeypatch.setattr(roomctl, "extensions",
                        lambda target=None, **kw: seen.update(kw) or {"ok": True})
    assert cli.main(["extension", "install", ID, ID2]) == 0
    assert seen["install"] == [ID, ID2]
    assert cli.main(["extension", "remove", ID]) == 0
    assert seen["remove"] == ID
    # `list` is the default, and takes neither
    assert cli.main(["extension"]) == 0


def test_firefox_says_so_instead_of_installing(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text('token = "t"\nhome_url = "about:blank"\n'
                   '[browser]\nautolaunch = false\nkind = "firefox"\n',
                   encoding="utf-8")
    monkeypatch.setenv("ROOM_CONFIG", str(cfg))
    with TestClient(appmod.app) as c:
        assert c.get("/v1/extensions", headers=AUTH).status_code == 501
        assert c.post("/v1/extensions", json={"ids": [ID]},
                      headers=AUTH).status_code == 501
        assert "extensions" not in c.get("/v1/status", headers=AUTH).json()["supports"]
