"""Run: pytest.

The one thing worth checking about the page without a browser: that every
`$("#x")` has a matching element. A renamed id fails silently — the button just
stops working, and nothing in the console says why — so it is exactly the
regression that survives a careful read.
"""

import re
from pathlib import Path

import pytest

PAGES = sorted((Path(__file__).parent.parent / "web").glob("*.html"))


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_selector_has_an_element(page):
    html = page.read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([\w-]+)"', html))
    used = set(re.findall(r'\$\("#([\w-]+)"\)', html))
    assert not used - ids, f"{page.name}: JS queries ids that do not exist"
    assert not ids - used, f"{page.name}: elements nothing ever queries"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_nothing_builds_dom_from_a_string(page):
    """This page holds the agent token and renders strings it does not control:
    a screenshot's `title` comes off whatever arbitrary site is on the wall. A
    page titled `<img onerror=…>` has to be a caption, not script."""
    sinks = re.findall(r"\b(innerHTML|outerHTML|insertAdjacentHTML|document\.write)\b",
                       re.sub(r"//.*", "", page.read_text(encoding="utf-8")))
    assert not sinks, f"{page.name}: use createElement + textContent, not {sinks[0]}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_no_timer_fetches_pictures(page):
    """The remote-desktop boundary, mechanically. A screenshot happens because
    someone asked for one; a panel that re-fetched on an interval would be a
    slow remote desktop, which is the one thing this is not. Adding a poller
    means adding a timer, so the timers are what this checks.
    """
    html = page.read_text(encoding="utf-8")
    timers = re.findall(r"set(?:Interval|Timeout)\(\s*([\w$]+)", html)
    allowed = {"refresh", "tick"}         # status polls; neither captures
    assert set(timers) <= allowed, f"{page.name}: unexpected timer {set(timers) - allowed}"
