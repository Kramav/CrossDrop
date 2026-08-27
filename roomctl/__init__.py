"""Client for the room-display agent. The CLI and eve both import this.

One target = one Pi: a base url and a bearer token, read from targets.toml.
Every call returns the agent's parsed JSON, so callers never touch httpx.

Failures surface as RuntimeError with the agent's own message attached — eve
should not have to know what an HTTPStatusError is to say "the display is down".
"""

import os
import tomllib
from pathlib import Path

import httpx

# Uploads cross a tailnet, not loopback, and a 25 MB PDF over Wi-Fi is not fast.
TIMEOUT = 60.0


def targets_path() -> Path:
    return Path(os.getenv("ROOMCTL_TARGETS") or Path(__file__).parent / "targets.toml")


def load_targets() -> dict:
    p = targets_path()
    if not p.exists():
        # ASCII only: the Windows console codepage mangles anything else.
        raise RuntimeError(f"{p}: no targets file - copy targets.example.toml next to it")
    # utf-8-sig: Windows editors and PowerShell write a BOM that tomllib chokes on.
    return tomllib.loads(p.read_text(encoding="utf-8-sig"))


def resolve(target: str | None = None) -> dict:
    """Pick a target: the named one, else `default`, else the only one there is."""
    t = load_targets()
    names = [k for k, v in t.items() if isinstance(v, dict)]
    name = target or t.get("default") or (names[0] if len(names) == 1 else None)
    if not name:
        raise RuntimeError(f"no target given and no default; targets: {', '.join(names) or 'none'}")
    if name not in names:
        raise RuntimeError(f"unknown target {name!r}; targets: {', '.join(names) or 'none'}")
    entry = t[name]
    for key in ("url", "token"):
        if not entry.get(key):
            raise RuntimeError(f"target {name!r} is missing {key}")
    return entry


def _call(target: str | None, method: str, path: str, **kw) -> dict:
    e = resolve(target)
    try:
        r = httpx.request(method, e["url"].rstrip("/") + path, timeout=TIMEOUT,
                          headers={"Authorization": f"Bearer {e['token']}"}, **kw)
    except httpx.HTTPError as exc:                      # unreachable Pi, DNS, timeout
        raise RuntimeError(f"{e['url']}: {exc}") from exc
    if r.status_code >= 400:
        # The agent puts the useful part in {"detail": ...}; fall back to raw body.
        detail = r.json().get("detail") if "json" in r.headers.get("content-type", "") else r.text
        raise RuntimeError(f"{path} -> {r.status_code}: {detail}")
    return r.json()


# A *target* is a Pi. A *screen* is one monitor attached to it. screen=None means
# that Pi's first screen, which is the whole API on a single-monitor display.
def status(target: str | None = None) -> dict:
    return _call(target, "GET", "/v1/status")


def screens(target: str | None = None) -> dict:
    return _call(target, "GET", "/v1/screens")


def navigate(url: str, target: str | None = None, screen: str | None = None) -> dict:
    return _call(target, "POST", "/v1/navigate", json={"url": url, "screen": screen})


def upload(path: str | Path, target: str | None = None,
           screen: str | None = None) -> dict:
    p = Path(path)
    with p.open("rb") as fh:
        # multipart, so `screen` rides along as a form field, not JSON
        data = {"screen": screen} if screen else None
        return _call(target, "POST", "/v1/upload",
                     files={"file": (p.name, fh)}, data=data)


def reload(target: str | None = None, screen: str | None = None) -> dict:
    return _call(target, "POST", "/v1/reload", json={"screen": screen})


def home(target: str | None = None, screen: str | None = None) -> dict:
    return _call(target, "POST", "/v1/home", json={"screen": screen})


def scroll(target: str | None = None, screen: str | None = None,
           dy: int = 600, to: str | None = None) -> dict:
    return _call(target, "POST", "/v1/scroll",
                 json={"screen": screen, "dy": dy, "to": to})


def autoscroll(action: str, target: str | None = None, screen: str | None = None,
               speed: int = 40) -> dict:
    return _call(target, "POST", "/v1/autoscroll",
                 json={"screen": screen, "action": action, "speed": speed})
