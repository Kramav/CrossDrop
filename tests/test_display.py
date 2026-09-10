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


# --- splitting a panel into two screens --------------------------------------

M1 = {"output": "HDMI-1", "position": "0,0", "size": "2560x1440"}
M2 = {"output": "HDMI-2", "position": "2560,0", "size": "1366x768"}


def test_an_output_nobody_split_passes_through_untouched():
    assert display.split([M1, M2], {}) == [M1, M2]
    assert display.split([M1, M2], None) == [M1, M2]


def test_halves_tile_the_panel_exactly():
    """No gap and no overlap. Overlap is the dangerous one: two windows at one
    origin is a tie _by_bounds has to refuse, which costs you /v1/input."""
    got = display.split([M1, M2], {"HDMI-1": "lr"})
    assert [s["output"] for s in got] == ["HDMI-1-L", "HDMI-1-R", "HDMI-2"]
    assert [s["position"] for s in got[:2]] == ["0,0", "1280,0"]
    assert [s["size"] for s in got[:2]] == ["1280x1440", "1280x1440"]
    # Left edge of the right half == right edge of the left half.
    assert 0 + 1280 == 1280
    assert all(s["fullscreen"] is False for s in got[:2])
    assert "fullscreen" not in got[2], "an unsplit monitor gained a flag"


def test_a_top_bottom_split_stacks_them():
    got = display.split([M1], {"HDMI-1": "tb"})
    assert [s["output"] for s in got] == ["HDMI-1-T", "HDMI-1-B"]
    assert [s["position"] for s in got] == ["0,0", "0,720"]
    assert [s["size"] for s in got] == ["2560x720", "2560x720"]


def test_an_odd_size_loses_no_pixel():
    """1365 -> 682 + 683, not 682 + 682. A dropped column is a one-pixel strip
    of desktop showing between two kiosk windows, forever."""
    odd = {"output": "X", "position": "10,20", "size": "1365x769"}
    lr = display.split([odd], {"X": "lr"})
    assert [s["size"] for s in lr] == ["682x769", "683x769"]
    assert lr[1]["position"] == "692,20"                    # 10 + 682
    tb = display.split([odd], {"X": "tb"})
    assert [s["size"] for s in tb] == ["1365x384", "1365x385"]
    assert tb[1]["position"] == "10,404"                    # 20 + 384


def test_halves_keep_their_place_in_the_left_to_right_order():
    """settings.apply falls back to index when a saved row has no output, so a
    split must not reshuffle the monitors around it."""
    got = display.split([M1, M2], {"HDMI-2": "lr"})
    assert [s["output"] for s in got] == ["HDMI-1", "HDMI-2-L", "HDMI-2-R"]


def test_a_nonsense_split_leaves_the_monitor_whole(capsys):
    """A typo must cost the split, not the display."""
    got = display.split([M1], {"HDMI-1": "diagonally"})
    assert got == [M1]
    assert "not a way to split" in capsys.readouterr().out
