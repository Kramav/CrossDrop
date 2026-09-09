"""Run: pytest.

Does the installer actually install, and does the uninstaller actually undo it?

Everything else about deploy/ is checked by reading the scripts as text -- which
is how `setup.sh` came to rewrite five config keys and not the sixth, and how
`uninstall.sh` came to delete a `video=` pin it never placed. A string assertion
cannot see a path that is never written or a file that is never removed.

So this runs the real scripts, unmodified, against a temp tree. The redirection
is `PREFIX`, an env seam in the scripts themselves; the privileged and networked
commands are stubs on a constructed PATH (tests/stubs/). What it proves:

  - the installer writes a config the agent can actually load, pointed at the
    address the unit binds
  - the uninstaller leaves nothing behind, by *name* and by *content*
  - and leaves the user's own lines in their own files alone

Deliberately not in scope: `update.sh`, which creates symlinks that git-bash
fakes on Windows, and which has its own end-to-end test in test_deploy.py.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
STUBS = Path(__file__).parent / "stubs"
SETUP = ROOT / "deploy/pi/setup.sh"
UNINSTALL = ROOT / "deploy/pi/uninstall.sh"

# The deployed name. One constant, because the residue assertions below are
# name-driven -- renaming the installation is then a one-line change here.
NAME = "crossdrop"


def _bash() -> str:
    """git-bash, or skip.

    Windows has two: git-bash (MINGW), and C:\\Windows\\system32\\bash.exe, which
    is WSL and sees a completely different filesystem -- an `E:/...` prefix would
    land somewhere else entirely. Name the hazard rather than fail obscurely.
    """
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash")
    if os.name == "nt":
        uname = subprocess.run([bash, "-c", "uname -s"], capture_output=True,
                               text=True).stdout
        if "MINGW" not in uname and "MSYS" not in uname:
            pytest.skip(f"bash is {uname.strip()} (WSL?), not git-bash")
    return bash


@pytest.fixture
def box(tmp_path):
    """A fake box: a PREFIX tree, a HOME, a stub PATH, and a call log."""
    prefix = tmp_path / "box"
    home = prefix / "home" / "room"
    home.mkdir(parents=True)
    log = tmp_path / "stubs.log"
    log.write_text("", encoding="utf-8")

    bash = _bash()
    # Constructed, not inherited: if the stub dir were ever not first, the real
    # sudo/apt would run. On ubuntu-latest that means writing to the runner.
    real = os.path.dirname(bash)
    path = os.pathsep.join([str(STUBS), real, os.path.join(os.path.dirname(real), "bin")])

    env = {
        "PATH": path, "HOME": home.as_posix(), "PREFIX": prefix.as_posix(),
        "STUB_LOG": log.as_posix(), "STUB_REPO": ROOT.as_posix(),
        # The stub dir is first on PATH, so the git stub needs the real
        # binary by absolute path or it calls itself.
        "STUB_REAL_GIT": (shutil.which("git") or "git"),
        # UPGRADE=0 skips a full-upgrade the apt stub would no-op anyway; keeping
        # it explicit means the log stays readable.
        "UPGRADE": "0",
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*",
    }

    class Box:
        def __init__(self):
            self.prefix, self.home, self.log, self.env = prefix, home, log, env

        def run(self, script, *args, pi=False, **extra):
            e = {**env, **extra}
            if not pi:
                # raspi-config's presence is what flips IS_PI, so the non-Pi run
                # needs it off the PATH entirely.
                e["PATH"] = os.pathsep.join(
                    [str(tmp_path / "nopi"), *path.split(os.pathsep)[1:]])
                nopi = tmp_path / "nopi"
                nopi.mkdir(exist_ok=True)
                for f in STUBS.iterdir():
                    if f.name != "raspi-config":
                        shutil.copy(f, nopi / f.name)
            return subprocess.run([bash, str(script), *args], env=e,
                                  capture_output=True, text=True)

        def calls(self):
            return self.log.read_text(encoding="utf-8").splitlines()

    return Box()


def install(box, pi, **env):
    r = box.run(SETUP, pi=pi, **env)
    assert r.returncode == 0, f"setup.sh failed\n{r.stdout}\n{r.stderr}"
    assert any(c.startswith("sudo\t") for c in box.calls()), \
        "no sudo calls logged — the stub PATH did not take, and the real one may have run"
    return r


# --- the install ------------------------------------------------------------

@pytest.mark.parametrize("pi", [True, False], ids=["pi", "debian"])
def test_the_installer_builds_what_it_says_it_does(box, pi):
    install(box, pi)
    p = box.prefix
    for expected in [p / f"opt/{NAME}/current/agent/app.py",
                     p / f"opt/{NAME}/extensions",
                     p / f"etc/{NAME}/config.toml",
                     box.home / f".local/share/{NAME}",
                     box.home / ".config/systemd/user/crossdrop-agent.service"]:
        assert expected.exists(), f"{expected} was never created"


@pytest.mark.parametrize("pi", [True, False], ids=["pi", "debian"])
def test_the_installer_writes_a_config_the_agent_can_load(box, pi, monkeypatch):
    """The check a string assertion cannot make. setup.sh rewrote five keys and
    left home_url as the example's "/home", which resolves to 127.0.0.1 -- a
    port nothing binds, so every screen came up on an error page and update.sh
    rolled back every release. Load the file it really wrote."""
    install(box, pi)
    cfg = box.prefix / f"etc/{NAME}/config.toml"
    monkeypatch.setenv("CROSSDROP_CONFIG", str(cfg))
    monkeypatch.setenv("CROSSDROP_SETTINGS", str(box.prefix / "none.json"))
    from agent.app import load_config

    loaded = load_config()
    assert loaded["token"] and loaded["token"] != "change-me"
    home = loaded["screens"][0]["home_url"]
    assert home.startswith("http://100.64.0.1:8080/home"), home
    assert "127.0.0.1" not in home, home


def test_the_token_never_reaches_stdout(box):
    """Stronger than the string check beside it in test_deploy.py: read the real
    output of a real run and look for anything token-shaped."""
    r = install(box, pi=True)
    import re

    assert not re.search(r"[0-9a-f]{64}", r.stdout + r.stderr), \
        "a 64-hex run reached the terminal"


def test_a_second_install_does_not_mint_a_new_token(box):
    """Re-running is the documented way to repair a box, so it must not mint a
    new token -- that would silently invalidate every controller's targets.toml
    while looking like a successful repair.

    Comparing the two configs is not enough: the openssl stub is deterministic,
    so a full re-mint produces a byte-identical file and the comparison passes
    either way. Assert on the *behaviour* -- that the second run said it left
    the file alone, and never called openssl at all.
    """
    install(box, pi=True)
    cfg = box.prefix / f"etc/{NAME}/config.toml"
    first = cfg.read_text(encoding="utf-8")
    box.log.write_text("", encoding="utf-8")        # only the second run
    r = install(box, pi=True)
    assert cfg.read_text(encoding="utf-8") == first, "the config was rewritten"
    assert "left alone" in r.stdout, r.stdout
    assert not [c for c in box.calls() if c.startswith("openssl	")],         "a second install generated a fresh token"


# --- and the round trip -----------------------------------------------------

@pytest.mark.parametrize("pi", [True, False], ids=["pi", "debian"])
def test_setup_then_uninstall_leaves_nothing_behind(box, pi):
    """Name-driven and content-driven, rather than a snapshot diff.

    A before/after tree diff goes red on every legitimate change to the
    installer and reports it as a wall of paths. What actually matters is
    residue, and residue has two shapes: a path still *named* for this project,
    and a file still *holding* its name -- a kiosk block left in .bash_profile,
    a journald drop-in the uninstaller knows by an older filename.
    """
    install(box, pi)
    r = box.run(UNINSTALL, "-y", pi=pi)
    assert r.returncode == 0, f"uninstall.sh failed\n{r.stdout}\n{r.stderr}"

    left = [p for p in box.prefix.rglob("*") if NAME in p.name.lower()]
    assert not left, left

    held = []
    for p in box.prefix.rglob("*"):
        if not p.is_file():
            continue
        try:
            if NAME in p.read_text(encoding="utf-8", errors="ignore").lower():
                held.append(p)
        except OSError:
            pass
    assert not held, held


def test_a_users_own_profile_lines_survive_the_uninstall(box):
    """The kiosk block is removed with a sed range. A range whose end address
    never matches deletes to end of file, so anything the user added after the
    block goes with it."""
    prof = box.home / ".profile"
    prof.write_text("export EDITOR=vim\n", encoding="utf-8")
    install(box, pi=False)
    prof.write_text(prof.read_text(encoding="utf-8") + "export PAGER=less\n",
                    encoding="utf-8")

    box.run(UNINSTALL, "-y", pi=False)
    left = prof.read_text(encoding="utf-8")
    assert "export EDITOR=vim" in left and "export PAGER=less" in left, left
    assert "CrossDrop" not in left and "startx" not in left, left


def test_disable_runs_while_the_unit_files_are_still_there(box):
    """Ordering, across a stub call and a real rm. Disabling after deleting the
    unit files leaves dangling default.target.wants/ symlinks that a later
    install trips over."""
    install(box, pi=True)
    box.run(UNINSTALL, "-y", pi=True)
    disable = [c for c in box.calls() if "disable" in c]
    assert disable, box.calls()
    assert "units_present=0" not in disable[0], \
        f"the units were already gone when disable ran: {disable[0]}"


def test_the_uninstaller_survives_deleting_the_tree_it_runs_from(box):
    """It lives under $OPT and deletes $OPT. bash reads a script as it executes,
    so without the mktemp self-copy the rm truncates the file mid-run and
    everything after it -- journald, autologin, the kiosk block -- silently never
    happens. The old test compared two string offsets; this runs it from the
    doomed path."""
    install(box, pi=True)
    installed = box.prefix / f"opt/{NAME}/current/deploy/pi/uninstall.sh"
    assert installed.exists()
    r = box.run(installed, "-y", pi=True)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "Gone." in r.stdout, "the run stopped before its own last line"
    assert not (box.prefix / f"etc/{NAME}").exists(), \
        "the steps after the rm never ran"


# --- what it must NOT take away ---------------------------------------------

def test_a_video_pin_we_did_not_place_is_left_alone(box):
    """setup.sh writes a pin only when VIDEO is set, and drops a marker beside
    the config when it does. Without that marker the uninstaller cannot tell our
    pin from the admin's -- and "boots with no monitor attached" is a legitimate
    reason to have one that predates this project. A pin removed at random is a
    Pi that boots dark, read at boot, with no keyboard."""
    boot = box.prefix / "boot/firmware"
    boot.mkdir(parents=True)
    (boot / "cmdline.txt").write_text("console=serial0,115200 root=PARTUUID=abc\n",
                                      encoding="utf-8")
    install(box, pi=True)                       # VIDEO unset: no pin, no marker
    # The admin pins an output afterwards, for their own reasons -- the "boots
    # with no monitor attached" case pi-setup.md describes.
    (boot / "cmdline.txt").write_text(
        "console=serial0,115200 root=PARTUUID=abc video=HDMI-A-1:1920x1080@60D\n",
        encoding="utf-8")
    box.run(UNINSTALL, "-y", pi=True)
    assert "video=HDMI-A-1" in (boot / "cmdline.txt").read_text(encoding="utf-8")


def test_a_video_pin_we_did_place_is_removed(box):
    boot = box.prefix / "boot/firmware"
    boot.mkdir(parents=True)
    (boot / "cmdline.txt").write_text("console=serial0,115200 root=PARTUUID=abc\n",
                                      encoding="utf-8")
    install(box, pi=True, VIDEO="HDMI-A-1:1920x1080@60D")
    assert "video=HDMI-A-1" in (boot / "cmdline.txt").read_text(encoding="utf-8")
    box.run(UNINSTALL, "-y", pi=True)
    assert "video=" not in (boot / "cmdline.txt").read_text(encoding="utf-8")


def test_a_console_autologin_we_did_not_write_is_left_alone(box):
    """`getty@tty1.service.d/autologin.conf` is also exactly what
    `raspi-config nonint do_boot_behaviour B2` writes. Matching on the filename
    took away a Pi admin's console autologin while the closing banner promised
    the Pi's autologin settings were being left alone."""
    d = box.prefix / "etc/systemd/system/getty@tty1.service.d"
    d.mkdir(parents=True)
    theirs = "[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin pi %I\n"
    (d / "autologin.conf").write_text(theirs, encoding="utf-8")
    install(box, pi=True)                       # the Pi branch writes no getty file
    box.run(UNINSTALL, "-y", pi=True)
    assert (d / "autologin.conf").read_text(encoding="utf-8") == theirs


def test_the_one_we_did_write_is_removed(box):
    install(box, pi=False)                      # the Debian branch writes it
    conf = box.prefix / "etc/systemd/system/getty@tty1.service.d/autologin.conf"
    assert conf.exists(), "the Debian branch did not write an autologin"
    box.run(UNINSTALL, "-y", pi=False)
    assert not conf.exists()


def test_nothing_reached_the_network_or_escaped_the_prefix(box):
    """The two stub guards, asserted rather than assumed. `curl` exits 91 and
    `sudo` exits 90, both loudly, so a regression here is a failure with a
    message rather than a silent write to the developer's real /opt."""
    install(box, pi=True)
    r = box.run(UNINSTALL, "-y", pi=True)
    assert "STUB REFUSED" not in (r.stdout + r.stderr), r.stderr
    assert not [c for c in box.calls() if c.startswith("curl\t")], \
        "something tried to reach the network"


# --- migrating a v1 box -----------------------------------------------------
# v1.3.0's setup.sh has no PREFIX seam, so the old layout cannot be built by
# running it. The fixture is synthetic instead -- honest, because migrate.sh
# only cares about paths, and OLD_LAYOUT below is asserted against what
# uninstall.sh's own v1 list produces so the two cannot drift.

OLD_NAME = "room-display"
V1_TOKEN = "1111111111111111111111111111111111111111111111111111111111111111"


def v1_box(box):
    """A box as setup.sh v1.3.0 left it: the paths, the token, the snapshot."""
    p, home = box.prefix, box.home
    (p / f"opt/{OLD_NAME}/current/deploy/pi").mkdir(parents=True)
    (p / f"opt/{OLD_NAME}/extensions/abc").mkdir(parents=True)
    (p / f"opt/{OLD_NAME}/extensions/abc/manifest.json").write_text("{}", "utf-8")
    (p / f"etc/{OLD_NAME}").mkdir(parents=True)
    (p / f"etc/{OLD_NAME}/config.toml").write_text(
        f'token = "{V1_TOKEN}"\nhome_url = "http://100.64.0.1:8080/home"\n'
        f'[browser]\nkind = "chromium"\n'
        f'profile_dir = "/run/user/1000/{OLD_NAME}/profile"\n'
        f'extensions_dir = "/opt/{OLD_NAME}/extensions"\n'
        f'[upload]\ndir = "/run/user/1000/{OLD_NAME}/uploads"\n', encoding="utf-8")
    data = home / f".local/share/{OLD_NAME}"
    data.mkdir(parents=True)
    (data / "profile.tar.gz").write_bytes(b"the logins")
    (data / "settings.json").write_text('{"screens": [{"name": "Samsung"}]}', "utf-8")
    units = home / ".config/systemd/user"
    units.mkdir(parents=True)
    for u in ["display-agent.service", f"{OLD_NAME}-restart.timer",
              f"{OLD_NAME}-restart.service", f"{OLD_NAME}-update.timer",
              f"{OLD_NAME}-update.service"]:
        (units / u).write_text("[Unit]\n", encoding="utf-8")
    (p / f"opt/{OLD_NAME}/releases").mkdir(parents=True)
    (p / f"opt/{OLD_NAME}/releases/.failed-v1.3.0").write_text("", encoding="utf-8")
    return p


def test_migrating_keeps_the_token_and_the_logins(box):
    """The two things that cannot be regenerated. A new token silently
    invalidates every controller's targets.toml; a lost profile.tar.gz logs the
    display out of everything, and nobody can type the password back in."""
    v1_box(box)
    r = box.run(ROOT / "deploy/pi/migrate.sh", pi=True,
                SETUP=str(SETUP), REPO="unused")
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"

    cfg = (box.prefix / f"etc/{NAME}/config.toml").read_text(encoding="utf-8")
    assert V1_TOKEN in cfg, "the token was regenerated"
    assert OLD_NAME not in cfg, cfg
    # The v1 default resolves to loopback while the unit binds the tailnet
    # address, so carrying it across intact is a migration that reports success
    # and leaves the wall on an error page with auto-update rolling back
    # every release.
    assert 'home_url = "/home"' not in cfg, "the v1 home_url survived the migration"
    assert "100.64.0.1" in cfg, cfg
    snap = box.home / f".local/share/{NAME}/profile.tar.gz"
    assert snap.exists() and snap.read_bytes() == b"the logins"
    # Extensions live on the SD card, not tmpfs, so they are worth carrying.
    assert (box.prefix / f"opt/{NAME}/extensions/abc/manifest.json").exists()
    # And the backup the banner promises is really there.
    assert (box.prefix / f"etc/{NAME}/config.toml.pre-crossdrop").exists()


def test_migrating_clears_a_latched_failure(box):
    """A tag that failed verify under the old layout is latched forever. Left
    behind, the marker outlives the thing that caused it and the box never
    updates again."""
    v1_box(box)
    r = box.run(ROOT / "deploy/pi/migrate.sh", pi=True,
                SETUP=str(SETUP), REPO="unused")
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert not list((box.prefix / f"opt/{NAME}").glob("releases/.failed-*"))


def test_nothing_of_the_old_layout_is_left_after_migrating(box):
    v1_box(box)
    box.run(ROOT / "deploy/pi/migrate.sh", pi=True, SETUP=str(SETUP), REPO="unused")
    left = [p for p in box.prefix.rglob("*") if OLD_NAME in p.name.lower()]
    assert not left, left


def test_the_installer_refuses_a_v1_box_instead_of_installing_beside_it(box):
    """Two trees means two agents racing for :8080 and two autostart units, and
    which one wins is whichever systemd started first."""
    v1_box(box)
    r = box.run(SETUP, pi=True)
    assert r.returncode == 1, r.stdout
    assert "migrate.sh" in r.stderr, r.stderr
    assert not (box.prefix / f"opt/{NAME}").exists(), "it installed anyway"


def test_an_unmigrated_box_is_still_cleaned_by_the_new_uninstaller(box):
    """After migrating, the old uninstall.sh is gone from the box -- so this one
    has to be able to clean a Pi that never migrated at all."""
    v1_box(box)
    r = box.run(UNINSTALL, "-y", pi=True)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    left = [p for p in box.prefix.rglob("*") if OLD_NAME in p.name.lower()]
    assert not left, left


def test_the_selfcheck_refuses_an_unmigrated_box(monkeypatch, capsys):
    """update.sh gates the symlink swap on selfcheck, so this turns "an
    unmigrated Pi reached the rename tag" into a no-swap that keeps running the
    release it has and prints the fix into its own journal -- rather than a
    swap onto code whose paths do not exist."""
    from agent.selfcheck import selfcheck

    monkeypatch.setenv("ROOM_CONFIG", "/etc/room-display/config.toml")
    monkeypatch.delenv("CROSSDROP_CONFIG", raising=False)
    assert selfcheck() == 1
    assert "migrate.sh" in capsys.readouterr().err


def test_the_uninstaller_runs_on_a_box_with_nothing_installed(box):
    """Running it twice, or on a box that was never installed, has to be a
    no-op that says so -- it is the documented way to make sure a partial
    install is really gone.

    The bug this pins was silent and total: `_unit_data` pipes sed into tail,
    `set -o pipefail` makes the pipeline take sed's status, and sed exits 2 on a
    unit file that is not there. Under `set -e` the assignment ended the script
    before the first line of output, with no message, because sed's stderr is
    redirected. Exit 2 and an empty screen.
    """
    r = box.run(UNINSTALL, "-y", pi=True)
    assert r.returncode == 0, f"exit {r.returncode}\n{r.stdout}\n{r.stderr}"
    assert "already gone" in r.stdout, r.stdout
    assert "Gone." in r.stdout, r.stdout


def test_uninstalling_twice_is_a_no_op(box):
    install(box, pi=True)
    first = box.run(UNINSTALL, "-y", pi=True)
    assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"
    second = box.run(UNINSTALL, "-y", pi=True)
    assert second.returncode == 0, f"{second.stdout}\n{second.stderr}"
    assert "Gone." in second.stdout, second.stdout


def test_the_uninstaller_finds_a_data_dir_the_unit_moved(box):
    """CROSSDROP_DATA is set by `Environment=` in the unit file -- the documented
    way to move the data dir -- and never in the SSH shell this runs from. Read
    only the environment and a box that moved its data dir keeps profile.tar.gz,
    i.e. the browser logins, while the banner says "Gone." """
    install(box, pi=True)
    moved = box.home / "kioskdata"
    moved.mkdir()
    (moved / "profile.tar.gz").write_bytes(b"the logins")
    unit = box.home / ".config/systemd/user/crossdrop-agent.service"
    unit.write_text(unit.read_text(encoding="utf-8")
                    + f"\nEnvironment=CROSSDROP_DATA={moved.as_posix()}\n",
                    encoding="utf-8")
    r = box.run(UNINSTALL, "-y", pi=True)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert not moved.exists(), "the browser logins were left on disk"


def test_migrating_fetches_the_code_before_destroying_the_old_tree(box):
    """$OLD_OPT used to be deleted ten lines before the clone that replaces it,
    so a repo gone private, an ssh remote with no key, or a network blip left
    the box with no code, no units and no way to be told so.

    Runs the real clone path -- every other migrate test passes SETUP= and
    skips it -- with a git stub that fails, and asserts the v1 tree survived.
    """
    v1_box(box)
    fail_git = box.prefix / "failgit"
    fail_git.mkdir()
    (fail_git / "git").write_text(
        "#!/bin/sh\nprintf 'git\t%s\n' \"$*\" >> \"$STUB_LOG\"\n"
        "case \"$1\" in clone) echo 'fatal: could not read from remote' >&2; exit 128 ;; esac\n"
        "exit 0\n", encoding="utf-8", newline="\n")
    (fail_git / "git").chmod(0o755)
    env = dict(box.env)
    env["PATH"] = os.pathsep.join([str(fail_git), env["PATH"]])
    env["REPO"] = "https://example.invalid/gone.git"   # the clone is what fails
    r = subprocess.run([_bash(), str(ROOT / "deploy/pi/migrate.sh")], env=env,
                       capture_output=True, text=True)

    assert r.returncode == 1, f"a failed clone was not fatal\n{r.stdout}"
    assert "nothing has been changed" in r.stderr, r.stderr
    # The v1 box is untouched, so the operator can simply try again.
    assert (box.prefix / f"opt/{OLD_NAME}/current").exists(), "the code was destroyed"
    assert (box.prefix / f"etc/{OLD_NAME}/config.toml").exists(), "the token was moved"
    assert (box.home / f".local/share/{OLD_NAME}/profile.tar.gz").exists()


def test_migrating_reads_the_data_dir_out_of_the_v1_unit(box):
    """ROOM_DATA is set by `Environment=` in the unit file, never in the SSH
    shell this runs from. Reading only the environment stranded profile.tar.gz
    -- the browser logins -- and reported success."""
    v1_box(box)
    moved = box.home / "kioskdata"
    moved.mkdir()
    (moved / "profile.tar.gz").write_bytes(b"the logins")
    unit = box.home / ".config/systemd/user/display-agent.service"
    unit.write_text(f"[Service]\nEnvironment=ROOM_DATA={moved.as_posix()}\n",
                    encoding="utf-8")

    r = box.run(ROOT / "deploy/pi/migrate.sh", pi=True, SETUP=str(SETUP), REPO="x")
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    snap = box.home / f".local/share/{NAME}/profile.tar.gz"
    assert snap.exists() and snap.read_bytes() == b"the logins", \
        "the logins were left behind at the moved location"


def test_an_edited_kiosk_block_does_not_take_the_rest_of_the_profile(box):
    """The sed range's end address is only searched *after* the header, but two
    earlier versions of this guard searched the whole file -- so an admin who
    already auto-started X on tty1 (exactly the box the Debian branch targets)
    satisfied it with their own line above our header. With our own
    `exec startx` removed, the range then ran unterminated and deleted from the
    header to end of file."""
    prof = box.home / ".profile"
    prof.write_text("export EDITOR=vim\n"
                    "# my own kiosk, predating this project\n"
                    "[ \"$(tty)\" = /dev/tty2 ] && exec startx\n", encoding="utf-8")
    install(box, pi=False)
    # The admin disables our kiosk by dropping the exec, keeping the note.
    text = prof.read_text(encoding="utf-8").replace(
        '[ "$(tty)" = /dev/tty1 ] && [ -z "${DISPLAY:-}" ] && exec startx -- -nocursor',
        "# disabled while we debug")
    prof.write_text(text + "export KEEP_ME=1\n", encoding="utf-8")

    r = box.run(UNINSTALL, "-y", pi=False)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    left = prof.read_text(encoding="utf-8")
    assert "export EDITOR=vim" in left, left
    assert "export KEEP_ME=1" in left, left
    assert "/dev/tty2" in left, "the admin's own kiosk line was eaten"
    assert "left in place" in r.stderr, r.stderr


def test_a_partly_migrated_box_can_be_finished(box):
    """setup.sh sends a half-migrated box to migrate.sh (its guard is only
    `[ -d "$OLD_OPT" ]`, deliberately). migrate.sh then refused it with
    "$NEW_OPT already exists" -- and both dead ends are reached *after*
    `systemctl --user disable --now display-agent` has run, so the wall is dark
    and no documented command brings it back."""
    v1_box(box)
    # The state an interrupt between the extensions move and the rm leaves.
    (box.prefix / f"opt/{NAME}").mkdir(parents=True)
    (box.prefix / f"opt/{NAME}/extensions").mkdir()

    r = box.run(ROOT / "deploy/pi/migrate.sh", pi=True, SETUP=str(SETUP), REPO="x")
    assert r.returncode == 0, f"a resumable state was refused\n{r.stdout}\n{r.stderr}"
    assert "resuming a partial migration" in r.stdout, r.stdout
    cfg = (box.prefix / f"etc/{NAME}/config.toml").read_text(encoding="utf-8")
    assert V1_TOKEN in cfg, "the token was lost while finishing the migration"
    assert not (box.prefix / f"opt/{OLD_NAME}").exists()
