"""Run: pytest.

What deploy/ assumes about the agent, and whether its scripts parse at all.

update.sh reads the agent's JSON with `sed` and `case`, because the Pi has no
`jq` and installing one puts another apt package on a box that updates itself
unattended. That makes the exact wire shape a contract between two files that
never import each other -- and the failure is silent: a renamed field means the
rollback check quietly stops checking, which nobody notices until the release it
was supposed to catch is on the wall.
"""

import base64
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.app import app

TOKEN = "test-token"
H = {"Authorization": f"Bearer {TOKEN}"}
SCRIPTS = sorted((Path(__file__).parent.parent / "deploy").rglob("*.sh"))
UPDATE_SH = (Path(__file__).parent.parent / "deploy/pi/update.sh").read_text(
    encoding="utf-8")


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_the_shell_scripts_parse(script):
    """A syntax error in these is discovered by a Pi, at 04:00, with no keyboard
    attached. `bash -n` is free."""
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash")
    r = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.fixture
def client(tmp_path, monkeypatch, request):
    p = tmp_path / "config.toml"
    p.write_text(f'token = "{TOKEN}"\nhome_url = "about:blank"\n'
                 f'[browser]\nkind = "chromium"\nautolaunch = false\n',
                 encoding="utf-8")
    monkeypatch.setenv("ROOM_CONFIG", str(p))
    with TestClient(app) as c:
        yield c


def test_inspect_reports_error_page_in_the_shape_update_sh_matches(client,
                                                                   monkeypatch):
    """update.sh greps the raw body for `"error_page":false`. FastAPI serialises
    JSON compactly, with no space after the colon -- if that ever changes, the
    rollback check silently matches nothing and stops guarding anything."""
    from agent import browser
    monkeypatch.setattr(browser, "inspect", lambda cfg, screen=None: {
        "url": "http://x/", "title": "t", "ready_state": "complete",
        "error_page": False, "has_media": False, "scroll_y": 0,
        "scroll_height": 10, "fields": []})
    raw = client.get("/v1/inspect", headers=H).text
    assert '"error_page":false' in raw
    # And the strings the script actually contains are the ones produced here.
    assert '*\'"error_page":false\'*' in UPDATE_SH
    assert '*\'"error_page":true\'*' in UPDATE_SH


def test_update_sh_can_pull_the_image_out_of_a_screenshot_reply(client,
                                                                monkeypatch):
    """The rollback diagnostic decodes the picture with sed + base64. Run the
    real pipeline against a real reply rather than trusting that it matches."""
    from agent import browser
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n-not-really-but-bytes").decode()
    monkeypatch.setattr(browser, "screenshot", lambda *a, **k: {
        "image": png, "format": "jpeg", "width": 8, "height": 8,
        "url": "http://x/", "title": "t"})
    raw = client.post("/v1/screenshot", headers=H, json={"format": "jpeg"}).text

    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash")
    # The exact pipeline from update.sh's snapshot_failure(). Binary in and out:
    # text mode would translate the newlines inside a real image.
    r = subprocess.run(
        [bash, "-c", r"""sed -n 's/.*"image":"\([^"]*\)".*/\1/p' | base64 -d"""],
        input=raw.encode(), capture_output=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout == base64.b64decode(png)
    assert r.stdout.startswith(b"\x89PNG")


def test_the_image_stays_first_in_the_reply(client, monkeypatch):
    """The sed above is greedy up to `"image":"`, so a *second* quoted field
    before it would be skipped harmlessly -- but base64 containing a quote would
    not be. It cannot: base64 has no quotes. This pins the assumption that the
    value is one unbroken quoted run."""
    from agent import browser
    monkeypatch.setattr(browser, "screenshot", lambda *a, **k: {
        "image": "AAAA", "format": "png", "width": 1, "height": 1,
        "url": 'http://x/?q="odd"', "title": 'a "quoted" title'})
    raw = client.post("/v1/screenshot", headers=H, json={}).text
    assert re.search(r'"image":"([A-Za-z0-9+/=]*)"', raw).group(1) == "AAAA"


def test_the_rollback_still_latches_and_snapshots_before_it(client):
    """Order matters: the picture has to be taken while the broken release is
    still the one running, i.e. before the symlink goes back."""
    latch = UPDATE_SH.index('touch "$RELEASES/.failed-$TAG"')
    snap = UPDATE_SH.index("snapshot_failure\n")
    restore = UPDATE_SH.index('ln -sfn "$PREV" "$CURRENT"')
    assert snap < latch < restore
