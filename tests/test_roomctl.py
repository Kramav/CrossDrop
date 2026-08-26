"""Run: pytest.

Drives roomctl against a real agent on a real socket, with autolaunch off — no
browser. That's the point: /v1/status answers `browser: "down"` and everything
else 503s, which exercises the client's transport, auth, target resolution and
error path without needing a kiosk. The kiosk half is covered by ROOM_SMOKE.
"""

import json
import threading
import time

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
    monkeypatch.setenv("ROOM_CONFIG", str(cfg))

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
    s = roomctl.status()
    assert s["up"] is True
    assert s["browser"] == "down", s      # no browser launched, and it says so


def test_bad_token_is_a_clean_error(agent):
    agent.write_text(agent.read_text(encoding="utf-8").replace(TOKEN, "wrong"), encoding="utf-8")
    with pytest.raises(RuntimeError, match="401"):
        roomctl.status()


def test_unknown_target_names_the_real_ones(agent):
    with pytest.raises(RuntimeError, match="unknown target 'kitchen'.*study"):
        roomctl.status("kitchen")


def test_missing_targets_file_says_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOMCTL_TARGETS", str(tmp_path / "nope.toml"))
    with pytest.raises(RuntimeError, match="targets.example.toml"):
        roomctl.status()


def test_cli_prints_json_and_exits_zero(agent, capsys):
    assert cli.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["up"] is True


def test_cli_reports_a_dead_browser_on_stderr(agent, capsys):
    # navigate needs a browser; there isn't one. The CLI must fail loudly, not
    # print a traceback and not exit 0 — scripts and eve branch on this.
    assert cli.main(["navigate", "https://example.com"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("roomctl:") and "503" in err, err
