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
