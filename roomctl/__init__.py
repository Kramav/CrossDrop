"""Client for the crossdrop agent. The CLI and eve both import this.

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
import sys
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


def config_path() -> Path:
    """~/.config/roomctl/targets.toml, or %APPDATA%\\roomctl\\targets.toml.

    This file holds bearer tokens. It used to default to `Path(__file__).parent`
    -- i.e. *inside the installed package*, which under a plain `pip install .`
    is site-packages: wiped on upgrade, not somewhere anyone looks for secrets,
    and world-readable by default.
    """
    if os.name == "nt" and os.getenv("APPDATA"):
        return Path(os.environ["APPDATA"]) / "roomctl" / "targets.toml"
    return Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config") \
        / "roomctl" / "targets.toml"


def targets_path() -> Path:
    """Where targets.toml is. ROOMCTL_TARGETS wins; then the config dir; then
    beside the package, which is where it used to live and where an existing
    install still has it."""
    if os.getenv("ROOMCTL_TARGETS"):
        return Path(os.environ["ROOMCTL_TARGETS"])
    beside = Path(__file__).parent / "targets.toml"
    cfg = config_path()
    # Legacy location, but only if it is really there: a checkout that has one is
    # still the file the developer is editing, and silently ignoring it would be
    # worse than keeping the fallback.
    if beside.exists() and not cfg.exists():
        return beside
    return cfg


def load_targets() -> dict:
    p = targets_path()
    if not p.exists():
        # ASCII only: the Windows console codepage mangles anything else.
        raise RuntimeError(f"{p}: no targets file - copy targets.example.toml there")
    # Tokens. Nothing enforces this on the file, so say so rather than leave a
    # credential readable by every account on a shared controller box.
    if os.name != "nt" and (p.stat().st_mode & 0o077):
        print(f"warning: {p} is readable by other users; chmod 600 it "
              f"(it holds bearer tokens)", file=sys.stderr)
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

    def window(self, state: str, screen: str | None = None) -> dict:
        """normal / minimized / fullscreen. Minimized frees the Pi's desktop
        without stopping the agent; fullscreen puts the kiosk back."""
        return self._call("POST", "/v1/window", json={"screen": screen, "state": state})

    def extensions(self, install: list[str] | None = None,
                   remove: str | None = None) -> dict:
        """List, install (Web Store ids) or remove a kiosk extension. Nothing is
        live until the browser restarts -- the reply says so in
        `pending_restart`."""
        if remove:
            return self._call("DELETE", f"/v1/extensions/{remove}")
        if install:
            return self._call("POST", "/v1/extensions", json={"ids": install})
        return self._call("GET", "/v1/extensions")

    def screenshot(self, screen: str | None = None, region: dict | None = None,
                   format: str = "png", quality: int = 80) -> dict:
        """What the screen is actually showing. `image` is base64.

        The read-back for everything else here: `navigate` reports the url it
        was handed, so a redirect or a login wall still looks like success.

        `region` is `{"x","y","width","height"}` in CSS pixels, clamped to the
        viewport. The returned `width`/`height` are what you got.

            shot = c.screenshot()
            Path("wall.png").write_bytes(base64.b64decode(shot["image"]))
        """
        return self._call("POST", "/v1/screenshot",
                          json={"screen": screen, "region": region,
                                "format": format, "quality": quality})

    def inspect(self, screen: str | None = None) -> dict:
        """What the page says about itself — no image, no field values.

        The one a program wants: `error_page` catches Chromium's own crash and
        network pages, which render fine and answer `/v1/status` with a 200.

            if c.inspect()["error_page"]:
                c.reload()
        """
        return self._call("GET", "/v1/inspect", params={"screen": screen})

    def input(self, actions: list[dict], screen: str | None = None,
              deadline_ms: int | None = None) -> dict:
        """Click, drag, type and press keys, in order, in one request.

        501 unless the agent's config.toml has `[interact] enabled = true` —
        check `"input" in status()["supports"]` first. Stops at the first
        failure; `results` says which action, and `ok` is false.

            c.input([{"do": "click", "selector": "#user"},
                     {"do": "type", "text": user},
                     {"do": "click", "selector": "#pass"},
                     {"do": "type", "text": password},
                     {"do": "key", "key": "Enter"}])
        """
        return self._call("POST", "/v1/input",
                          json={"screen": screen, "actions": actions,
                                "deadline_ms": deadline_ms})

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
    """A Client for a named target from targets.toml.

        with roomctl.client("study") as c:
            c.navigate("https://example.com")

    This replaced fourteen module-level functions (`roomctl.status()`,
    `roomctl.navigate(url, target)`, …) that each did exactly
    `with client(target) as c: return c.method(...)`. Every new endpoint was
    written three times — Client method, module function, CLI entry — and the
    middle one only ever reordered arguments. Two lines here do the same job,
    and the CLI now holds one connection for a command instead of dialling
    inside every lambda.
    """
    e = resolve(target)
    return Client(e["url"], e["token"])
