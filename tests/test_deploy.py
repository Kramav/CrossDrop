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
import os
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


SETUP_SH = (Path(__file__).parent.parent / "deploy/pi/setup.sh").read_text(
    encoding="utf-8")


SNAPSHOT_SH = (Path(__file__).parent.parent / "deploy/pi/profile-snapshot.sh"
               ).read_text(encoding="utf-8")


def test_the_agent_and_the_snapshot_script_agree_on_the_data_dir():
    """Each carries its own copy of the default, because one is Python and the
    other is bash and neither can import the other. A box where they disagree
    snapshots into a directory the agent never reads — which looks exactly like
    a working install right up until the reboot that needed the snapshot."""
    from agent import settings

    assert settings.DATA_DIR == ".local/share/room-display"
    assert f'$HOME/{settings.DATA_DIR}' in SNAPSHOT_SH, \
        "profile-snapshot.sh's default has drifted from settings.DATA_DIR"


def test_both_halves_move_together(monkeypatch, tmp_path):
    """One variable, not two. systemd applies Environment= to ExecStartPre and
    ExecStopPost as well, so setting it in the unit moves the agent and the
    snapshot script at once."""
    from agent import settings

    monkeypatch.delenv("ROOM_SETTINGS", raising=False)
    monkeypatch.setenv("ROOM_DATA", str(tmp_path / "elsewhere"))
    assert settings.data_dir() == tmp_path / "elsewhere"
    assert settings.path() == tmp_path / "elsewhere" / "settings.json"
    assert settings.last_path() == tmp_path / "elsewhere" / "last.json"
    assert 'ROOM_DATA:-' in SNAPSHOT_SH, "the script ignores ROOM_DATA"
    # And the unit documents it, so the two are discoverable together.
    unit = (Path(__file__).parent.parent / "deploy/pi/display-agent.service"
            ).read_text(encoding="utf-8")
    assert "ROOM_DATA" in unit


def test_room_settings_still_wins(monkeypatch, tmp_path):
    """It predates ROOM_DATA and the whole suite points it at a tmp_path."""
    from agent import settings

    monkeypatch.setenv("ROOM_DATA", str(tmp_path / "dir"))
    monkeypatch.setenv("ROOM_SETTINGS", str(tmp_path / "explicit.json"))
    assert settings.path() == tmp_path / "explicit.json"


def test_the_installer_never_prints_the_token():
    """It used to, for the convenience of a copy-pasteable curl — which also
    wrote the one credential this box has into terminal scrollback, a `script`
    log, and whatever the emulator keeps. Printing the command that reads it
    costs one step and leaves the secret in the file it already lives in."""
    # The token is never read into a variable at all, so there is nothing for
    # the heredoc to interpolate even by accident.
    assert 'TOKEN="$(' not in SETUP_SH, "the installer still captures the token"
    banner = SETUP_SH[SETUP_SH.index("Done. From a controller box"):]
    assert "\\$TOKEN" in banner, "the placeholder should stay unexpanded"
    # The reader is shown the command that reads it, and runs it themselves.
    assert "sed -n" in banner


def test_the_installer_stops_if_pip_fails():
    """It ran under `set -e` with no message of its own: a wheel that failed to
    build left a half-populated venv, the script carried on and enabled the
    service, and you learned about it from a journalctl dump at the end."""
    assert "if ! .venv/bin/pip install" in SETUP_SH
    assert "not enabling the service" in SETUP_SH


def test_the_installer_says_it_is_enabling_tailscale_ssh():
    """It made every install SSH-reachable under tailnet ACLs the script never
    mentioned. Defensible, but it should be a stated decision rather than a
    silent one inside a `curl | bash`."""
    assert "TSSSH" in SETUP_SH
    said = SETUP_SH.index("enabling Tailscale SSH")
    assert said < SETUP_SH.index("tailscale up --ssh"), "said after the fact"


def test_full_upgrade_can_be_declined():
    assert "UPGRADE" in SETUP_SH and "UPGRADE=0 to skip" in SETUP_SH


def test_tag_verification_is_opt_in_and_a_hard_gate():
    """Off by default: turning it on without a signing key in place would stop
    every Pi updating, and a display stuck on an old release is worse than the
    risk it removes. On, it must refuse rather than warn."""
    assert 'VERIFY_TAG:-0' in UPDATE_SH, "not off by default"
    block = UPDATE_SH[UPDATE_SH.index("VERIFY_TAG:-0"):]
    block = block[:block.index("# --- 3")]
    assert "verify-tag" in block and "exit 1" in block
    # And it latches, or the timer retries the same bad tag every 30 minutes.
    assert ".failed-$TAG" in block
    # The latch needs its directory to exist on a first-ever run.
    assert block.index('mkdir -p "$RELEASES"') < block.index('touch "$RELEASES')


UNINSTALL_SH = (Path(__file__).parent.parent / "deploy/pi/uninstall.sh").read_text(
    encoding="utf-8")


def test_the_uninstaller_removes_exactly_the_block_the_installer_appends(tmp_path):
    """setup.sh appends a kiosk block to ~/.profile; uninstall.sh strips it with
    a sed range. The two live in different files and nothing else pins them
    together — a reworded first line leaves the block behind, and the next
    `startx` fights the reinstalled one for tty1.

    Run the real sed against the real block, with a line of the user's own on
    either side to prove the range does not eat them."""
    block = SETUP_SH[SETUP_SH.index("# CrossDrop kiosk session"):]
    block = block[:block.index("\nEOF") + 1]
    prof = tmp_path / ".profile"
    prof.write_text(f'export EDITOR=vim\n\n{block}\nexport PAGER=less\n',
                    encoding="utf-8")

    r = _sh("-c", f"sed -i '/^# CrossDrop kiosk session/,/exec startx/d' "
                  f"'{prof.as_posix()}'")
    assert r.returncode == 0, r.stderr

    left = prof.read_text(encoding="utf-8")
    assert "CrossDrop" not in left and "startx" not in left, left
    assert "export EDITOR=vim" in left and "export PAGER=less" in left
    # And the script still contains that exact sed.
    assert "/^# CrossDrop kiosk session/,/exec startx/d" in UNINSTALL_SH


def test_the_uninstaller_does_not_delete_itself_mid_run():
    """It lives under /opt/room-display and deletes /opt/room-display. bash reads
    a script as it executes, so without the copy the `rm -rf` truncates the file
    it is running from and the rest — journald, autologin, the pin — silently
    never happens."""
    copy = UNINSTALL_SH.index('cp "$SELF" "$TMP"')
    assert copy < UNINSTALL_SH.index("sudo rm -rf /opt/room-display")
    assert 'exec env ROOM_UNINSTALL_TMP=' in UNINSTALL_SH


def _sh(*args, **kw):
    """Run bash, or skip. Windows dev boxes have it via git; the Pi is bash."""
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash")
    return subprocess.run([bash, *args], capture_output=True, text=True, **kw)


def test_the_token_is_read_as_one_line(tmp_path):
    """update.sh's health check greps the token out of config.toml with sed. A
    second matching line -- a commented-out old token, a [section] that has one
    too -- made TOKEN multi-line, the Authorization header malformed, every
    probe 401, and the release roll back for no reason at all. A *false*
    rollback on a box with no keyboard is the expensive failure here."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('token = "real-token"\nhome_url = "/home"\n'
                   '[old]\ntoken = "stale-token"\n', encoding="utf-8")
    r = _sh("-c", 'sed -n \'s|^token = "\\(.*\\)"|\\1|p' + f'\' "{cfg.as_posix()}" | head -1')
    assert r.stdout.strip() == "real-token", r.stdout
    assert "| head -1" in UPDATE_SH, "the script itself still takes every match"


def test_verify_tag_actually_refuses_an_unsigned_tag(tmp_path):
    """The signing path was never run -- not in CI, not in the smoke docs -- so
    "VERIFY_TAG=1 is a hard gate" was a claim about a string in a file. This
    runs the real script against a real repo with a real unsigned tag.

    The refusal, not the acceptance: signing needs a GPG key, and generating one
    is slow and flaky. Refusing is the half that has to be right anyway -- an
    accept-by-default here is push access to the repo becoming code execution on
    every Pi within 30 minutes.
    """
    if not shutil.which("git"):
        pytest.skip("no git")
    repo, root = tmp_path / "repo", tmp_path / "root"
    repo.mkdir()
    # gpgsign off: a dev box with global commit signing on would otherwise fail
    # to build the *fixture*. The tag stays lightweight either way, which is
    # exactly what verify-tag has to refuse.
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t",
           "-c", "commit.gpgsign=false", "-C", str(repo)]
    subprocess.run([*git[:1], "init", "-q", str(repo)], check=True,
                   capture_output=True)
    (repo / "f.txt").write_text("hi", encoding="utf-8")
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-qm", "x"], check=True, capture_output=True)
    subprocess.run([*git, "tag", "v9.9.9"], check=True, capture_output=True)

    r = _sh(str(Path(__file__).parent.parent / "deploy/pi/update.sh"),
            env={**os.environ, "ROOT": root.as_posix(),
                 "REPO": repo.as_posix(), "VERIFY_TAG": "1",
                 "CFG": str(tmp_path / "nope.toml"), "PATH": os.environ["PATH"]})

    assert r.returncode == 1, f"an unsigned tag was accepted\n{r.stdout}{r.stderr}"
    assert "not a validly signed tag" in r.stderr, r.stderr
    # Latched, or the timer redeploys the same bad tag every 30 minutes.
    assert (root / "releases" / ".failed-v9.9.9").exists(), "no latch marker"
    # And nothing was swapped: `current` never appeared.
    assert not (root / "current").exists(), "a refused tag reached the swap"


def test_the_rollback_still_latches_and_snapshots_before_it(client):
    """Order matters: the picture has to be taken while the broken release is
    still the one running, i.e. before the symlink goes back.

    Scoped to the rollback section rather than the whole file — the signature
    check latches too, and searching from the top found *its* touch instead.
    """
    section = UPDATE_SH[UPDATE_SH.index("# --- 7. rollback"):]
    latch = section.index('touch "$RELEASES/.failed-$TAG"')
    snap = section.index("snapshot_failure\n")
    restore = section.index('ln -sfn "$PREV" "$CURRENT"')
    assert snap < latch < restore
