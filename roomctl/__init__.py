"""Client for the room-display agent. The CLI and eve both import this.

One target = one Pi: a base url and a bearer token. Two ways in:

    roomctl.status()                          # named target from targets.toml
    roomctl.Client(url, token).status()       # url + token you already hold

The second exists because a program driving this usually has the url and token
already — in an env var, a CI secret, a vault — and should not have to write a
TOML file to disk to use a library. Prefer it for anything long-lived: it holds
one connection open instead of dialling per call.

Every call returns the agent's parsed JSON, so callers never touch httpx.

Failures surface as AgentError, which *is* a RuntimeError — old `except
RuntimeError` keeps working — carrying `.status` and `.detail` so a caller can
branch on the kind of failure without reading English:

    try: c.scroll()
    except roomctl.Unsupported: ...      # 501, this browser can't
    except roomctl.Unreachable: ...      # the box is off
"""

import os
import tomllib
from pathlib import Path
from typing import Self

import httpx

# Uploads cross a tailnet, not loopback, and a 25 MB PDF over Wi-Fi is not fast.
TIMEOUT = 60.0


class AgentError(RuntimeError):
    """A call reached a verdict we can name. `status` is the HTTP code, or 0 if
    we never got one."""

    def __init__(self, status: int, detail: str, where: str):
        self.status, self.detail = status, detail
        super().__init__(f"{where} -> {status}: {detail}" if status
                         else f"{where}: {detail}")


class Unreachable(AgentError):
    """No answer at all: box off, DNS, tailnet down, timeout."""


class NotFound(AgentError):
    """404 — no such screen, file, or nothing playing."""


class Unsupported(AgentError):
    """501 — this agent's browser cannot do that. Check status()["supports"]
    first and you will not see this."""


class Unavailable(AgentError):
    """503 — the agent is up but its browser is not."""


_BY_STATUS = {404: NotFound, 501: Unsupported, 503: Unavailable}


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


# A *target* is a Pi. A *screen* is one monitor attached to it. screen=None means
# that Pi's first screen, which is the whole API on a single-monitor display.
class Client:
    """One agent, one connection. Reusable and thread-safe (httpx.Client is).

    Long-lived callers should hold one and close it; `with Client(...) as c` does
    that. The module-level functions below open and close one per call, which is
    what they always did.
    """

    def __init__(self, url: str, token: str, timeout: float = TIMEOUT):
        self.url = url.rstrip("/")
        self._c = httpx.Client(base_url=self.url, timeout=timeout,
                               headers={"Authorization": f"Bearer {token}"})

    def close(self) -> None:
        self._c.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _call(self, method: str, path: str, **kw) -> dict:
        try:
            r = self._c.request(method, path, **kw)
        except httpx.HTTPError as exc:                  # unreachable Pi, DNS, timeout
            raise Unreachable(0, str(exc), self.url) from exc
        if r.status_code >= 400:
            # The agent puts the useful part in {"detail": ...}; fall back to raw body.
            detail = (r.json().get("detail")
                      if "json" in r.headers.get("content-type", "") else r.text)
            raise _BY_STATUS.get(r.status_code, AgentError)(r.status_code, detail, path)
        return r.json()

    def status(self) -> dict:
        """Also the capability probe: `kind`, `supports` and `started_at` say
        which half of this API exists and whether the agent has restarted."""
        return self._call("GET", "/v1/status")

    def screens(self) -> dict:
        return self._call("GET", "/v1/screens")

    def navigate(self, url: str, screen: str | None = None) -> dict:
        return self._call("POST", "/v1/navigate", json={"url": url, "screen": screen})

    def upload(self, path: str | Path, screen: str | None = None,
               navigate: bool = True) -> dict:
        """Send a file and show it. `navigate=False` stages it and returns the
        url without putting it on the wall."""
        p = Path(path)
        with p.open("rb") as fh:
            # multipart, so these ride along as form fields, not JSON
            data = {"navigate": str(navigate).lower()}
            if screen:
                data["screen"] = screen
            return self._call("POST", "/v1/upload",
                              files={"file": (p.name, fh)}, data=data)

    def reload(self, screen: str | None = None) -> dict:
        return self._call("POST", "/v1/reload", json={"screen": screen})

    def home(self, screen: str | None = None) -> dict:
        return self._call("POST", "/v1/home", json={"screen": screen})

    def scroll(self, screen: str | None = None, dy: int = 600,
               to: str | None = None) -> dict:
        return self._call("POST", "/v1/scroll",
                          json={"screen": screen, "dy": dy, "to": to})

    def autoscroll(self, action: str, screen: str | None = None,
                   speed: int = 40) -> dict:
        return self._call("POST", "/v1/autoscroll",
                          json={"screen": screen, "action": action, "speed": speed})

    def media(self, action: str = "state", screen: str | None = None,
              value: int = 0) -> dict:
        """play / pause / toggle / mute / unmute / seek (seconds) / volume (0-100),
        or "state" to just ask. 404s when the page has no video or audio."""
        return self._call("POST", "/v1/media",
                          json={"screen": screen, "action": action, "value": value})


def client(target: str | None = None) -> Client:
    """A Client for a named target from targets.toml."""
    e = resolve(target)
    return Client(e["url"], e["token"])


# The by-name API: same signatures it has always had, one connection per call.
# `Client` is the one to hold if you are calling more than once.
def status(target: str | None = None) -> dict:
    with client(target) as c:
        return c.status()


def screens(target: str | None = None) -> dict:
    with client(target) as c:
        return c.screens()


def navigate(url: str, target: str | None = None, screen: str | None = None) -> dict:
    with client(target) as c:
        return c.navigate(url, screen)


def upload(path: str | Path, target: str | None = None,
           screen: str | None = None, navigate: bool = True) -> dict:
    with client(target) as c:
        return c.upload(path, screen, navigate)


def reload(target: str | None = None, screen: str | None = None) -> dict:
    with client(target) as c:
        return c.reload(screen)


def home(target: str | None = None, screen: str | None = None) -> dict:
    with client(target) as c:
        return c.home(screen)


def scroll(target: str | None = None, screen: str | None = None,
           dy: int = 600, to: str | None = None) -> dict:
    with client(target) as c:
        return c.scroll(screen, dy, to)


def autoscroll(action: str, target: str | None = None, screen: str | None = None,
               speed: int = 40) -> dict:
    with client(target) as c:
        return c.autoscroll(action, screen, speed)


def media(action: str = "state", target: str | None = None, screen: str | None = None,
          value: int = 0) -> dict:
    """play / pause / toggle / mute / unmute / seek (seconds) / volume (0-100),
    or "state" to just ask. 404s when the page has no video or audio."""
    with client(target) as c:
        return c.media(action, screen, value)
