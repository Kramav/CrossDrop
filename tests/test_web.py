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
