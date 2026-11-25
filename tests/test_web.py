"""Run: pytest.

The one thing worth checking about the page without a browser: that every
`$("#x")` has a matching element. A renamed id fails silently — the button just
stops working, and nothing in the console says why — so it is exactly the
regression that survives a careful read.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGES = sorted((Path(__file__).parent.parent / "web").glob("*.html"))


HEX = re.compile(r"^[0-9a-fA-F]{3,8}$")


def referenced(html: str) -> tuple[set, set]:
    """(ids the JS looks up, ids anything refers to at all).

    CSS counts as a reference: a layout hook like #bar is used by the stylesheet
    and by nothing else, and contorting it into a class to satisfy a test would
    be the test wagging the page. Colour literals are dropped, or `#fff` reads
    as an element named fff.
    """
    js = set(re.findall(r'\$\("#([\w-]+)"\)', html))
    style = re.search(r"<style>(.*?)</style>", html, re.S)
    css = {m for m in re.findall(r"#([\w-]+)", style.group(1) if style else "")
           if not HEX.match(m)}
    return js, js | css


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_selector_has_an_element(page):
    html = page.read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([\w-]+)"', html))
    js, all_refs = referenced(html)
    assert not js - ids, f"{page.name}: JS queries ids that do not exist: {js - ids}"
    assert not ids - all_refs, \
        f"{page.name}: elements nothing refers to: {ids - all_refs}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_nothing_builds_dom_from_a_string(page):
    """This page holds the agent token and renders strings it does not control:
    a screenshot's `title` comes off whatever arbitrary site is on the wall. A
    page titled `<img onerror=…>` has to be a caption, not script."""
    sinks = re.findall(r"\b(innerHTML|outerHTML|insertAdjacentHTML|document\.write)\b",
                       re.sub(r"//.*", "", page.read_text(encoding="utf-8")))
    assert not sinks, f"{page.name}: use createElement + textContent, not {sinks[0]}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_nothing_captures_on_a_recurring_timer(page):
    """The remote-desktop boundary, mechanically.

    `setInterval` is the primitive that makes a page keep doing something on its
    own, so that is the one held to a fixed list: a capture on a repeating timer
    is a slow remote desktop, which is the one thing this is not.

    One-shot `setTimeout` is deliberately *not* restricted — it is how the page
    sleeps while a click settles, inside an action somebody took. What keeps
    that honest is the rule below it: no timer callback may reach a capture.
    """
    html = page.read_text(encoding="utf-8")
    repeating = set(re.findall(r"setInterval\(\s*([\w$]+)", html))
    allowed = {"refresh", "tick"}         # status polls; neither captures
    assert repeating <= allowed, f"{page.name}: recurring {repeating - allowed}"
    for name in repeating:
        body = re.search(rf"(?:async )?function {name}\(.*?\n}}", html, re.S)
        assert body, f"{page.name}: {name}() runs on a timer but is not a function"
        assert "/v1/screenshot" not in body.group(0), \
            f"{page.name}: {name}() runs on a timer and captures"


def test_stale_marking_hangs_off_the_action_not_the_request():
    """The bug this replaces: markStale() was hooked into req() on "any non-GET".
    But non-GET is not the same as changed-something — probeMedia() reads the
    player with a POST and runs on the 15s poll, so every fresh capture was
    marked stale the moment it was taken and again every fifteen seconds after.
    act() is the honest signal: it wraps exactly the user-initiated actions.
    """
    html = (PAGES[-1].parent / "index.html").read_text(encoding="utf-8")
    req = re.search(r"async function req\(.*?\n}", html, re.S).group(0)
    assert "markStale" not in req, "markStale is back in req(); it belongs in act()"
    act = re.search(r"async function act\(.*?\n}", html, re.S).group(0)
    assert "markStale" in act, "nothing marks a picture stale when you act"


# --- the one piece of page logic worth executing ----------------------------
# Clicking the screenshot is how a login gets typed into on a box with no
# keyboard, and the mapping from picture to page is a scale factor: get it wrong
# and every click misses by a constant, which looks exactly like the click never
# arriving. Nobody can eyeball a wrong scale factor, so it is run instead.

CASES = [
    # rect (where CSS put the image), shot (the capture's own pixels), click, expect
    ("shown at its true size", {"left": 0, "top": 0, "width": 1920, "height": 1080},
     {"width": 1920, "height": 1080}, (960, 540), (960, 540)),
    ("halved to fit the column", {"left": 0, "top": 0, "width": 960, "height": 540},
     {"width": 1920, "height": 1080}, (480, 270), (960, 540)),
    ("offset down the page", {"left": 100, "top": 200, "width": 960, "height": 540},
     {"width": 1920, "height": 1080}, (100, 200), (0, 0)),
    ("bottom-right corner", {"left": 0, "top": 0, "width": 480, "height": 270},
     {"width": 1920, "height": 1080}, (480, 270), (1920, 1080)),
    ("a tall portrait monitor", {"left": 10, "top": 10, "width": 270, "height": 480},
     {"width": 1080, "height": 1920}, (145, 250), (540, 960)),
]


@pytest.mark.parametrize("name,rect,shot,click,expect", CASES,
                         ids=[c[0] for c in CASES])
def test_a_click_on_the_picture_maps_onto_the_page(name, rect, shot, click, expect):
    node = shutil.which("node")
    if not node:
        pytest.skip("no node")
    src = re.search(r"function imagePoint\(.*?\n}", (PAGES[-1].parent / "index.html")
                    .read_text(encoding="utf-8"), re.S)
    assert src, "imagePoint went missing from index.html"
    script = (f"{src.group(0)}\n"
              f"const p = imagePoint({json.dumps(rect)}, {json.dumps(shot)}, "
              f"{click[0]}, {click[1]});\n"
              f"console.log(JSON.stringify(p));")
    r = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert (got["x"], got["y"]) == expect


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_the_script_parses(page, tmp_path):
    """The whole page is one <script>, so a syntax error anywhere in it is not a
    broken feature -- it is every feature, at once.

    Nothing runs, no handler is ever attached, and the symptom is a page that
    renders perfectly and ignores every button. That is what a duplicate
    declaration did: `const stage = $("#stage")` had been there for ages, a
    `function stage(change)` was added beside it, and `const` plus `function` of
    the same name is a SyntaxError at parse time -- before line one executes.

    Counting braces cannot see that. Only a parser can, so borrow node's.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("no node to parse with")
    html = page.read_text(encoding="utf-8")
    parts = html.split("<script>")
    assert len(parts) == 2, f"{page.name}: expected exactly one <script> block"
    js = tmp_path / (page.stem + ".js")
    js.write_text(parts[1].split("</script>")[0], encoding="utf-8")
    r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, f"{page.name} does not parse:\n{r.stderr}"


# The split preview recomputes display.split's geometry in the page, so the map
# can show a layout before anyone confirms it. Two copies of one formula drift;
# this runs the page's copy against the agent's for every layout in the table.

@pytest.mark.parametrize("now", [None, "grid", "lcr"])
@pytest.mark.parametrize("how", ["lr", "tb", "lcr", "grid"])
def test_the_split_preview_matches_what_the_agent_builds(how, now):
    """`now` is how the panel is cut today: re-cutting has to put the panel back
    together from its parts first, and get the same answer as cutting it whole."""
    from agent import display

    node = shutil.which("node")
    if not node:
        pytest.skip("no node")
    html = (PAGES[-1].parent / "index.html").read_text(encoding="utf-8")
    fns = [re.search(rf"function {n}\(.*?\n}}", html, re.S).group(0)
           for n in ("previewScreens", "bounds")]
    names = re.search(r"const LAYOUT_NAMES = .*?;\n", html, re.S).group(0)
    lay = re.search(r"const layouts = .*?;\n", html, re.S).group(0)
    m = {"output": "HDMI-1", "position": "7,3", "size": "1367x771"}
    today = display.split([m], {"HDMI-1": now} if now else {})
    settings = {"screens": [{"name": s["output"], **s} for s in today],
                "outputs": ["HDMI-1"],
                "layouts": {k: [c, r, list(lb)] for k, (c, r, lb) in display.SPLITS.items()}}
    script = (f"{names}{lay}{fns[0]}\n{fns[1]}\n"
              f"var settingsNow = {json.dumps(settings)};\n"
              f"var pending = {{ splits: {{ 'HDMI-1': {json.dumps(how)} }} }};\n"
              "console.log(JSON.stringify(previewScreens()));")
    r = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    got = [(s["output"], s["position"], s["size"]) for s in json.loads(r.stdout)]
    want = [(s["output"], s["position"], s["size"]) for s in display.split([m], {"HDMI-1": how})]
    assert got == want



def test_the_split_preview_handles_several_monitors_with_lookalike_names():
    """HDMI-1 beside HDMI-10, and an MST connector named DP-1-1: a part of
    HDMI-10 must never be mistaken for a part of HDMI-1. Each monitor starts
    cut one way and is previewed cut another."""
    from agent import display

    node = shutil.which("node")
    if not node:
        pytest.skip("no node")
    html = (PAGES[-1].parent / "index.html").read_text(encoding="utf-8")
    fns = "\n".join(re.search(rf"function {n}\(.*?\n}}", html, re.S).group(0)
                    for n in ("previewScreens", "bounds"))
    consts = "".join(re.search(rf"const {n} = .*?;\n", html, re.S).group(0)
                     for n in ("LAYOUT_NAMES", "layouts"))
    mons = [{"output": "HDMI-1", "position": "0,0", "size": "1920x1080"},
            {"output": "HDMI-10", "position": "1920,0", "size": "1366x768"},
            {"output": "DP-1-1", "position": "3286,0", "size": "2560x1440"}]
    now = {"HDMI-1": "grid", "HDMI-10": "lr"}
    then = {"HDMI-1": "lcr", "DP-1-1": "tb"}
    today = display.split(mons, now)
    settings = {"screens": [{"name": s["output"], **s} for s in today],
                "outputs": [m["output"] for m in mons],
                "layouts": {k: [c, r, list(lb)] for k, (c, r, lb) in display.SPLITS.items()}}
    script = (f"{consts}{fns}\nvar settingsNow = {json.dumps(settings)};\n"
              f"var pending = {{ splits: {json.dumps(then)} }};\n"
              "console.log(JSON.stringify(previewScreens()));")
    r = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    got = [(s["output"], s["position"], s["size"]) for s in json.loads(r.stdout)]
    want = [(s["output"], s["position"], s["size"]) for s in display.split(mons, then)]
    assert got == want

# The sender's sound source. The one rule that matters: no real microphone goes
# to the wall unless someone picked it. Run against fake devices, because the
# failure it guards -- Windows' "default" pointing at a webcam -- needs no
# hardware to reproduce, only the wrong logic.

AUDIO_HARNESS = r"""
const store = {};
const localStorage = new Proxy(store, { deleteProperty: (t, k) => (delete t[k], true) });
const opened = [], stopped = [];
const navigator = { mediaDevices: {
  async getUserMedia(c) {
    const want = c.audio.deviceId ? c.audio.deviceId.exact : DEFAULT;
    if (!DEVICES.some(d => d.deviceId === want) && want !== "default") throw new Error("gone");
    const real = want === "default" ? DEFAULT_REAL : want;
    opened.push(want);
    const label = (DEVICES.find(d => d.deviceId === real) || {}).label;
    const track = { label, getSettings: () => ({ deviceId: want }), stop: () => stopped.push(want) };
    return { getAudioTracks: () => [track] };
  },
  async enumerateDevices() { return DEVICES; },
}};
const el = () => ({ replaceChildren() {}, value: "", hidden: true });
const els = {};
const $ = s => (els[s] ||= el());
const document = { createElement: () => ({}) };
"""


def run_audio(case: dict) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("no node")
    html = (PAGES[-1].parent / "index.html").read_text(encoding="utf-8")
    consts = "".join(re.search(rf"const {n} = .*?;\n", html, re.S).group(0)
                     for n in ("LOOPBACK", "RAW", "PSEUDO"))
    fns = "\n".join(re.search(rf"(?:async )?function {n}\(.*?\n}}", html, re.S).group(0)
                    for n in ("openInput", "loopbackAudio", "drawAudioChoices"))
    script = (f"const DEVICES = {json.dumps(case['devices'])};\n"
              f"const DEFAULT = 'default';\n"
              f"const DEFAULT_REAL = {json.dumps(case['default'])};\n"
              f"{AUDIO_HARNESS}{consts}{fns}\n"
              f"Object.assign(store, {json.dumps(case.get('saved', {}))});\n"
              "loopbackAudio().then(t => console.log(JSON.stringify({"
              " got: t ? t.getSettings().deviceId : null, saved: store.audioDevice || null,"
              " opened, stopped })));")
    r = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


MIX = {"kind": "audioinput", "deviceId": "mix1", "label": "Stereo Mix (Realtek Audio)"}
CAM = {"kind": "audioinput", "deviceId": "cam1", "label": "Microphone (HD Webcam)"}
DEF = {"kind": "audioinput", "deviceId": "default", "label": "Default - Microphone (HD Webcam)"}


def test_the_default_device_is_never_what_gets_sent():
    """Windows' default is the webcam today. The automatic choice must open
    the Stereo Mix by its real id, and stop the webcam it opened to read labels."""
    got = run_audio({"devices": [DEF, CAM, MIX], "default": "cam1"})
    assert got["got"] == "mix1", got
    assert "default" in got["stopped"], got
    assert got["saved"] is None, "the automatic choice was remembered"


def test_no_loopback_means_no_sound_not_the_microphone():
    got = run_audio({"devices": [DEF, CAM], "default": "cam1"})
    assert got["got"] is None, got
    assert got["stopped"] == ["default"], got


def test_a_device_someone_picked_is_used():
    got = run_audio({"devices": [DEF, CAM, MIX], "default": "cam1",
                     "saved": {"audioDevice": "cam1"}})
    assert got["got"] == "cam1" and got["saved"] == "cam1", got


def test_no_sound_asks_for_nothing():
    got = run_audio({"devices": [DEF, CAM, MIX], "default": "cam1",
                     "saved": {"audioDevice": "none"}})
    assert got == {"got": None, "saved": "none", "opened": [], "stopped": []}


def test_a_remembered_default_from_before_is_not_trusted():
    """The old code could have saved "default". A picked device that has gone
    is forgotten, and the choice falls back to automatic."""
    got = run_audio({"devices": [DEF, CAM, MIX], "default": "cam1",
                     "saved": {"audioDevice": "unplugged-usb"}})
    assert got["got"] == "mix1" and got["saved"] is None, got


def test_a_saved_pseudo_device_is_dropped():
    """An earlier build could save "default" itself. Opened later, that is
    whatever Windows calls default then -- here, the webcam."""
    got = run_audio({"devices": [DEF, CAM, MIX], "default": "cam1",
                     "saved": {"audioDevice": "default"}})
    assert got["got"] == "mix1" and got["saved"] is None, got
