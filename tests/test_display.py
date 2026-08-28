"""Run: pytest.

Display power, with xset/xrandr stubbed and the clock faked. What matters is the
*policy* — that a screen showing something keeps the monitors up, that they go
down once everything is idle, and that any activity brings them back — plus the
one parser that rots if a tool's output ever changes.
"""

import pytest

from agent import display

# Real output from the Pi (Xorg, HDMI-1 1366x768 beside HDMI-2 2560x1440).
LISTMONITORS = """Monitors: 2
 0: +*HDMI-1 1366/609x768/347+0+0  HDMI-1
 1: +HDMI-2 2560/597x1440/336+1366+0  HDMI-2
"""


@pytest.fixture
def xset(monkeypatch):
    """Stub the X tools. Yields the list of argv lists that went out."""
    calls = []

    def run(argv):
        calls.append(argv)
        return LISTMONITORS if argv[0] == "xrandr" else ""

    monkeypatch.setattr(display, "_run", run)
    monkeypatch.setattr(display, "_on", True)
    monkeypatch.setattr(display, "_last", {})
    monkeypatch.setattr(display, "_content", {})
    return calls


@pytest.fixture
def clock(monkeypatch):
    """A monotonic clock we can wind forward, in minutes."""
    now = [1000.0]
    monkeypatch.setattr(display.time, "monotonic", lambda: now[0])
    return lambda minutes: now.__setitem__(0, now[0] + minutes * 60)


def make_cfg(**display_cfg):
    return {
        "display": display.DEFAULTS | display_cfg,
        "screens": [{"name": n, "home_url": f"http://pi/home?screen={n}"}
                    for n in ("left", "right")],
    }


def forced(calls):
    return [c[-1] for c in calls if c[:3] == ["xset", "dpms", "force"]]


# --- policy -----------------------------------------------------------------

def test_content_holds_the_display_up_then_sleeps(xset, clock):
    cfg = make_cfg()
    left, right = cfg["screens"]
    display.touch(left, "https://example.com/paper.pdf")   # showing something
    display.touch(right, right["home_url"])                # idle
    display.watch(cfg).set()                               # seed clocks, don't run

    clock(11)                                              # past idle_off_minutes
    assert not display._all_idle(cfg), "a screen showing content must hold it up"

    clock(110)                                             # past content_off_minutes
    assert display._all_idle(cfg)
    display.power(False)
    assert forced(xset) == ["off"]


def test_both_idle_sleeps_and_activity_wakes(xset, clock):
    cfg = make_cfg()
    left, right = cfg["screens"]
    for s in cfg["screens"]:
        display.touch(s, s["home_url"])                    # both sent home
    clock(11)
    assert display._all_idle(cfg)
    display.power(False)

    display.touch(left, "https://example.com/")            # anything you send
    assert forced(xset) == ["off", "on"]                   # wakes it, once each
    assert not display._all_idle(cfg)


def test_zero_disables_the_timer(xset, clock):
    cfg = make_cfg(idle_off_minutes=0)
    for s in cfg["screens"]:
        display.touch(s, s["home_url"])
    clock(10_000)
    assert not display._all_idle(cfg)


def test_claim_zeroes_the_timeouts_and_wakes(xset):
    display.claim()
    assert ["xset", "+dpms"] in xset
    assert ["xset", "dpms", "0", "0", "0"] in xset
    assert ["xset", "s", "off"] in xset
    # The display may well be dark when the agent starts -- that is the bug this
    # exists for -- so claim() must not assume it is on.
    assert forced(xset) == ["on"]


# --- parsing ----------------------------------------------------------------

def test_detect_reads_name_position_and_size(xset):
    assert display.detect() == [
        {"output": "HDMI-1", "position": "0,0", "size": "1366x768"},
        {"output": "HDMI-2", "position": "1366,0", "size": "2560x1440"},
    ]


def test_detect_survives_no_x(monkeypatch):
    monkeypatch.setattr(display, "_run", lambda argv: None)
    assert display.detect() == []
