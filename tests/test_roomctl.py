"""Run: pytest.

Drives roomctl against a real agent on a real socket, with autolaunch off — no
browser. That's the point: /v1/status answers `browser: "down"` and everything
else 503s, which exercises the client's transport, auth, target resolution and
error path without needing a kiosk. The kiosk half is covered by CROSSDROP_SMOKE.
"""

import json
import threading
import time
from pathlib import Path

import pytest
import uvicorn

import roomctl
from agent.app import app
from roomctl import cli

TOKEN = "test-token"


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """A live agent + a targets.toml pointing at it. Yields the targets path."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'token = "{TOKEN}"\nhome_url = "about:blank"\n'
                   f"[browser]\nautolaunch = false\n"
                   f"[upload]\nmax_mb = 1\nkeep = 2\n", encoding="utf-8")
    monkeypatch.setenv("CROSSDROP_CONFIG", str(cfg))

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]

    targets = tmp_path / "targets.toml"
    targets.write_text(f'default = "study"\n\n[study]\n'
                       f'url = "http://127.0.0.1:{port}"\ntoken = "{TOKEN}"\n', encoding="utf-8")
    monkeypatch.setenv("ROOMCTL_TARGETS", str(targets))
    yield targets

    server.should_exit = True
    thread.join(10)


def test_status_via_default_target(agent):
    with roomctl.client() as c:
        s = c.status()
    assert s["up"] is True
    assert s["browser"] == "down", s      # no browser launched, and it says so


def test_bad_token_is_a_clean_error(agent):
    agent.write_text(agent.read_text(encoding="utf-8").replace(TOKEN, "wrong"), encoding="utf-8")
    with pytest.raises(RuntimeError, match="401"), roomctl.client() as c:
        c.status()


def test_unknown_target_names_the_real_ones(agent):
    with pytest.raises(RuntimeError, match="unknown target 'kitchen'.*study"):
        roomctl.client("kitchen")


def test_missing_targets_file_says_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOMCTL_TARGETS", str(tmp_path / "nope.toml"))
    with pytest.raises(RuntimeError, match="targets.example.toml"):
        roomctl.client()


def test_cli_prints_json_and_exits_zero(agent, capsys):
    assert cli.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["up"] is True


def test_cli_reports_a_dead_browser_on_stderr(agent, capsys):
    # navigate needs a browser; there isn't one. The CLI must fail loudly, not
    # print a traceback and not exit 0 — scripts and eve branch on this.
    assert cli.main(["navigate", "https://example.com"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("roomctl:") and "503" in err, err


# --- where the flags go -----------------------------------------------------

@pytest.mark.parametrize("argv, screen, target", [
    (["navigate", "https://e.test", "-s", "right"], "right", None),
    (["navigate", "https://e.test", "--screen", "right"], "right", None),
    (["-s", "right", "navigate", "https://e.test"], "right", None),
    (["upload", "f.pdf", "--screen", "left"], "left", None),
    (["-t", "spare", "status"], None, "spare"),
    (["status", "-t", "spare"], None, "spare"),
    (["status"], None, None),
])
def test_the_screen_and_target_flags_parse_on_either_side_of_the_verb(
        monkeypatch, cli_target, capsys, argv, screen, target):
    """README and deploy/pi/README document eight commands in the form
    `roomctl navigate URL -s right`. Every one of them exited 2 with
    "unrecognized arguments": -t and -s were on the top-level parser only.

    Both positions have to work, and the fix that breaks the other one is easy
    to write -- a subparser copies its whole namespace over the outer one, so an
    ordinary default on the subcommand's -s silently overwrites a -s given
    before the verb. This runs all seven forms.
    """
    seen = {}
    # navigate(url, screen) and upload(path, screen) both take the screen
    # second; status() takes none, so those cases only pin the target.
    monkeypatch.setattr(roomctl.Client, "navigate",
                        lambda self, url, s=None: seen.update(screen=s) or {})
    monkeypatch.setattr(roomctl.Client, "upload",
                        lambda self, p, s=None, navigate=True:
                        seen.update(screen=s) or {})
    monkeypatch.setattr(roomctl.Client, "status", lambda self: {})
    monkeypatch.setattr(roomctl, "resolve",
                        lambda t=None: seen.update(target=t) or
                        {"url": "http://127.0.0.1:1", "token": "x"})

    assert cli.main(argv) == 0, capsys.readouterr().err
    capsys.readouterr()
    assert seen["target"] == target, seen
    if argv[0] != "status" and "status" not in argv:
        assert seen["screen"] == screen, seen


def test_the_targets_file_is_not_kept_inside_the_installed_package(monkeypatch):
    """It holds bearer tokens. The default used to be Path(__file__).parent --
    site-packages under a plain `pip install .`: wiped on upgrade, not somewhere
    anyone looks for a secret, and world-readable by default."""
    monkeypatch.delenv("ROOMCTL_TARGETS", raising=False)
    pkg = Path(roomctl.__file__).parent
    assert pkg not in roomctl.config_path().parents, roomctl.config_path()
    assert roomctl.config_path().name == "targets.toml"
    assert "roomctl" in roomctl.config_path().parent.name
