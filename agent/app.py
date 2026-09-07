"""Room display agent — frozen /v1 contract (PLAN.md §5)."""

import contextlib
import logging
import os
import secrets
import threading
import time
import tomllib
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

from . import browser, display, extensions, settings, storage

DEFAULTS = {
    "kind": "firefox", "path": "", "profile_dir": "", "autolaunch": True,
    "debug_port": 9222, "disk_cache_mb": 100, "extensions_dir": "",
}
UPLOAD_DEFAULTS = {"dir": "", "max_mb": 25, "keep": 5}
# POST /v1/input, off unless config.toml turns it on: the only route that acts
# *as* whoever the kiosk is logged in as. In config.toml and not settings.json
# because a switch the API can flip for itself is not a switch.
INTERACT_DEFAULTS = {"enabled": False, "max_actions": 40, "deadline_ms": 30_000}
# Where to bind. Loopback by default; the Pi sets its tailnet address (PLAN.md
# §10 — never 0.0.0.0). Here and not only in the systemd unit, so something
# other than systemd can start the agent.
SERVER_DEFAULTS = {"host": "127.0.0.1", "port": 8080}
SCREEN_DEFAULTS = {"name": "", "position": "", "size": "", "home_url": ""}

# To stderr, which under display-agent.service is journald:
# `journalctl --user -u display-agent`. Without it "the wall was showing the
# wrong thing at 9am" had no way of being answered after the fact.
log = logging.getLogger("room")

# First gap between kiosk launch attempts; doubles to five minutes. Overridable
# because how slow a session is to come up is a property of the box, not the
# code -- same as browser.py's PLACE_SETTLE.
LAUNCH_RETRY_SECS = float(os.getenv("ROOM_LAUNCH_RETRY", "5"))


def setup_logging() -> None:
    """Give the root logger a handler, once, and set the level on ours only.

    uvicorn leaves root alone, so without a handler everything below WARNING
    falls through to logging.lastResort and is dropped. basicConfig is a no-op
    if a handler exists, so a lifespan the tests run repeatedly is safe.

    The level goes on `room`, not root: root at INFO turns on every library at
    INFO, and httpx alone narrates one line per request. On a Pi whose journal
    is 32M and in RAM (deploy/pi/journald-volatile.conf) that is our own audit
    trail evicted by somebody else's chatter. ROOM_LOG=DEBUG adds the reads.
    """
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.setLevel(os.getenv("ROOM_LOG", "INFO").upper())


def load_config(path: str | os.PathLike | None = None) -> dict:
    path = Path(path or os.getenv("ROOM_CONFIG") or Path(__file__).parent / "config.toml")
    # utf-8-sig: Windows editors and PowerShell write a BOM that tomllib chokes on.
    cfg = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    cfg["browser"] = DEFAULTS | cfg.get("browser", {})
    cfg["upload"] = UPLOAD_DEFAULTS | cfg.get("upload", {})
    cfg["server"] = SERVER_DEFAULTS | cfg.get("server", {})
    cfg["display"] = display.DEFAULTS | cfg.get("display", {})
    cfg["interact"] = INTERACT_DEFAULTS | cfg.get("interact", {})
    if not cfg["browser"]["profile_dir"]:
        cfg["browser"]["profile_dir"] = str(path.parent / "profile")
    if not cfg["upload"]["dir"]:
        cfg["upload"]["dir"] = str(path.parent / "uploads")
    if not cfg.get("token"):
        raise RuntimeError(f"{path}: token is required")
    # Warn, never raise: a bad token must not be why a keyboard-less display
    # fails to boot. But an HTTP header is latin-1, so a non-ASCII token cannot
    # be *sent* -- every request 401s while the config looks perfectly fine.
    if not str(cfg["token"]).isascii():
        log.warning("%s: token has non-ASCII characters in it, so it can never "
                    "be sent in an Authorization header -- every request will "
                    "401. Use hex or base64: openssl rand -hex 32", path)

    # No [[screen]] blocks -> ask X, so a fresh install drives every connected
    # monitor with no config edit. Explicit blocks win; nothing detected
    # (Windows, no DISPLAY) -> one screen called "main", as it always was.
    blocks = cfg.get("screen") or [
        {"name": d["output"], "position": d["position"], "size": d["size"]}
        for d in display.detect()
    ]
    cfg["screens"] = [SCREEN_DEFAULTS | s for s in (blocks or [{}])]
    # Saved edits from the web UI, before names and home urls are finalised
    # below — a renamed screen has to get its new name stamped into its home url.
    settings.apply(cfg)
    for i, s in enumerate(cfg["screens"]):
        s["name"] = s["name"] or ("main" if i == 0 else f"screen{i + 1}")
        s["home_url"] = _home_url(cfg, s["home_url"] or cfg.get("home_url", "about:blank"))
        # The idle page names its monitor, and the url is the only way it can
        # know -- every window shares one profile and one debug port. Match on
        # the path: a url ending "?x=1" still points at /home.
        u = urlparse(s["home_url"])
        if u.path.rstrip("/").endswith("/home"):
            # Re-stamp, never just append: renaming a screen has to move the
            # name on its idle page too, and the previous ?screen= is still
            # sitting in the url we just loaded back from settings.json.
            q = [(k, v) for k, v in parse_qsl(u.query) if k != "screen"]
            q.append(("screen", s["name"]))
            s["home_url"] = urlunparse(u._replace(query=urlencode(q)))
    names = [s["name"] for s in cfg["screens"]]
    if len(set(names)) != len(names):
        raise RuntimeError(f"{path}: duplicate screen names {names}")
    return cfg


def _home_url(cfg: dict, url: str) -> str:
    """The one definition of `home_url` -- there used to be three, and they
    disagreed. A path is resolved against where we bind, so "/home" really is
    this agent's idle page; anything else has to be http, https or about:, the
    allowlist /v1/navigate has always had (PLAN.md §10). Raising matches the
    checks either side, and selfcheck runs this before a release is swapped in.
    """
    if url.startswith("/"):
        srv = cfg["server"]
        return f"http://{srv['host'] or '127.0.0.1'}:{srv['port']}{url}"
    if not url.startswith(("http://", "https://", "about:")):
        raise RuntimeError(
            f"home_url {url!r} must be http, https, about:blank, or a path "
            f"like \"/home\" for this agent's own idle page")
    return url


def swap_config(cfg: dict, fresh: dict) -> None:
    """Replace the contents of `cfg` with `fresh`, in place.

    In place, not reassigned: display.watch() closed over this dict at startup.

    Overwrite first, drop stale keys after, so no key present in both configs is
    ever *absent*. Browser routes are `def`, so a concurrent request really can
    read this mid-swap; `clear()` then `update()` left it empty in between and a
    reader got a KeyError and a 500 out of a route that had done nothing wrong.
    Each dict operation is atomic, so the worst seen now is one key from the old
    config beside one from the new.
    """
    cfg.update(fresh)
    for stale in set(cfg) - set(fresh):
        cfg.pop(stale, None)


def screen_of(cfg: dict, name: str | None) -> dict:
    """Resolve a screen name to its config. None -> the first one, so every
    caller that predates multi-monitor keeps hitting the same display."""
    if not name:
        return cfg["screens"][0]
    for s in cfg["screens"]:
        if s["name"] == name:
            return s
    known = ", ".join(s["name"] for s in cfg["screens"])
    raise HTTPException(404, f"no screen {name!r}; have: {known}")


def targets(cfg: dict, name: str | None) -> list[dict]:
    """The screens one request applies to. "all" fans out to every monitor."""
    return list(cfg["screens"]) if name == "all" else [screen_of(cfg, name)]


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    app.state.cfg = cfg = load_config()
    # Reported by /v1/status: autoscroll state is not persisted, so this is how
    # a poller tells "still running" from "the 04:00 restart threw it away".
    app.state.started_at = time.time()
    # We own the kiosk only if we started it. ROOM_SELFCHECK boots this app
    # beside the *live* kiosk to prove a new release starts, so it must never
    # launch a second browser onto that port or screen.
    launch = cfg["browser"]["autolaunch"] and not os.getenv("ROOM_SELFCHECK")
    # On app.state, not a local: _launch() fills it in from another thread.
    app.state.proc = None
    app.state.launch_error = ""
    app.state.stopping = threading.Event()
    watching = None
    if launch:
        # Only when we own the kiosk -- selfcheck must not blank real monitors.
        watching = display.watch(cfg)
        # Handed over, not looked up: reading app.state.stopping each pass let a
        # thread from a *previous* lifespan see the next one's fresh event and
        # go on launching browsers for an agent that had stopped. The launch
        # runs here, off the startup path, because lifespan blocks the port from
        # binding and update.sh rolls back if /v1/status is silent for 30s.
        threading.Thread(target=_launch, args=(cfg, app.state.stopping),
                         daemon=True).start()
    yield
    app.state.stopping.set()
    for name in list(_autoscroll):
        _autoscroll_stop(name)
    if watching:
        watching.set()
    if app.state.proc:
        _save_shown(cfg)                   # while the browser can still be asked
        browser.stop(cfg, app.state.proc)  # ours: take the whole tree down with us
    else:
        browser.close()  # not ours: just release the session and leave it running


def _launch(cfg: dict, stopping: threading.Event) -> None:
    """Start the kiosk browser, then put each screen on its home page.

    Retries, and never lets the failure out. Inline in lifespan, anything that
    raised took uvicorn down with it -- and the /v1/status that would have named
    the cause with it. So the API comes up regardless, reports `error`, and the
    transient case (the compositor is not up yet) heals itself.
    """
    delay = LAUNCH_RETRY_SECS
    while not stopping.is_set():
        try:
            app.state.proc = browser.launch(cfg)
        except Exception as e:
            app.state.launch_error = f"{type(e).__name__}: {e}"
            log.error("browser launch failed, retrying in %.0fs: %s", delay, e)
            if stopping.wait(delay):
                return
            delay = min(delay * 2, 300.0)   # backs off to 5 min, then stays there
            continue
        app.state.launch_error = ""
        # Shutdown may have run while launch() sat in its 30s wait_ready(),
        # seen proc as None and left the browser alone. Both sides stopping it
        # is harmless; neither is an orphaned fullscreen kiosk, forever.
        if stopping.is_set():
            browser.stop(cfg, app.state.proc)
            return
        log.info("browser launched")
        _home_when_ready(cfg)
        return


def _home_when_ready(cfg: dict) -> None:
    """Send each screen to its home page, waiting first for our *own* port.

    uvicorn binds the socket *after* this lifespan, so a kiosk pointed at our
    own /home during launch renders "can't be reached" and stays there. The
    probe is therefore a home_url pointing at our /home -- on the Pi the full
    tailnet url, since the unit passes `--host $(tailscale ip -4)` and
    [server].host is not what it bound.

    It used to probe the first http url of *any* host, then return without
    navigating if it never answered -- so an external home being down left the
    screen this exists to protect on the error page. Best-effort now, and every
    screen is navigated either way.
    """
    display.claim()          # take DPMS off the session before anything can blank
    probe = next((s["home_url"] for s in cfg["screens"]
                  if urlparse(s["home_url"]).path.rstrip("/").endswith("/home")), None)
    for _ in range(60 if probe else 0):
        try:
            httpx.get(probe, timeout=2)
            break
        except httpx.HTTPError:
            time.sleep(1)
    shown = {}
    with contextlib.suppress(Exception):    # a mangled last.json costs the
        shown = _restorable(cfg)            # restore, never the boot
    for s in cfg["screens"]:
        with contextlib.suppress(OSError, RuntimeError):
            browser.navigate(cfg, shown.get(s["name"]) or s["home_url"], s["name"])


def _save_shown(cfg: dict) -> None:
    """What each screen is showing, for the next start to put back. Once, at
    shutdown: this file is on the SD card, and not writing to that is most of
    what Phase 6 is about."""
    out = {}
    for s in cfg["screens"]:
        with contextlib.suppress(Exception):
            url = browser.current_url(cfg, s["name"])
            if url and url != s["home_url"]:
                out[s["name"]] = {"url": url, "at": display.last_active(s["name"])}
    with contextlib.suppress(OSError):
        settings.save({"screens": out}, settings.last_path())


def _restorable(cfg: dict) -> dict[str, str]:
    """Screen name -> the url it was showing, for screens still worth restoring.

    The nightly restart (deploy/pi/room-display-restart.timer) stops Chromium
    running the Pi out of memory, but must not clear the wall: something put up
    at 5pm should still be up in the morning, something from last week not.
    """
    minutes = cfg["display"]["restore_within_minutes"]
    if not minutes:
        return {}
    cutoff = time.time() - minutes * 60
    saved = settings.load(settings.last_path()).get("screens") or {}
    return {name: rec["url"] for name, rec in saved.items()
            if rec.get("url") and rec.get("at", 0) > cutoff and _still_there(cfg, rec["url"])}


def _still_there(cfg: dict, url: str) -> bool:
    """Uploads are tmpfs, so a reboot empties the store while last.json goes on
    pointing into it. Restoring a dead /files url puts a 404 on the wall, which
    is worse than the home page it replaced."""
    _, sep, file_id = url.partition("/files/")
    if not sep:
        return True                         # not ours; the site can answer for itself
    try:
        storage.path(cfg, file_id)
    except KeyError:
        return False
    return True


app = FastAPI(title="room-display agent", version="1", lifespan=lifespan)
_bearer = HTTPBearer(auto_error=True)


@app.middleware("http")
async def access_log(request: Request, call_next):
    """One line per request: what was asked, of what, and how it went.

    Mutations at INFO, reads at DEBUG -- the UI polls /v1/status every 15s and
    the kiosk polls /home-status, so reads at INFO would bury the one navigate
    you are looking for under thousands of lines a day, in a 32M journal that
    lives in RAM (deploy/pi/journald-volatile.conf).

    The body is deliberately not read: consuming it here starves the route, and
    re-injecting a stream to log a screen name is not worth the bugs. Route plus
    status code says enough.
    """
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("%s %s -> unhandled in %.0fms", request.method,
                      request.url.path, (time.monotonic() - started) * 1000)
        raise
    log.log(logging.DEBUG if request.method == "GET" else logging.INFO,
            "%s %s -> %d in %.0fms", request.method, request.url.path,
            response.status_code, (time.monotonic() - started) * 1000)
    return response


def auth(app_cfg: HTTPAuthorizationCredentials = Depends(_bearer)) -> None:
    # Bytes, not str: compare_digest refuses two str arguments unless both are
    # pure ASCII, so a non-ASCII token in config.toml raised TypeError out of
    # the dependency and surfaced as a 500. A wrong token has to be a 401
    # whatever it is made of, not "the agent is broken".
    if not secrets.compare_digest(app_cfg.credentials.encode("utf-8"),
                                  str(app.state.cfg["token"]).encode("utf-8")):
        raise HTTPException(401, "bad token")


class NavigateIn(BaseModel):
    url: AnyHttpUrl  # http/https only — scheme allowlist per PLAN.md §10
    screen: str | None = None       # None = the first screen; "all" = every one


class ScreenIn(BaseModel):
    screen: str | None = None


class ScrollIn(BaseModel):
    screen: str | None = None
    dy: int = 600                   # ~a screenful; negative scrolls up
    to: str | None = None           # "top" | "bottom", overrides dy


class AutoScrollIn(BaseModel):
    screen: str | None = None
    action: str                     # "start" | "stop"
    speed: int = 40                 # pixels per tick, ~10 ticks a second


class MediaIn(BaseModel):
    screen: str | None = None
    action: str = "state"           # see browser.MEDIA_ACTIONS
    value: int = 0                  # seek: seconds, +/-. volume: 0-100.


class DisplayIn(BaseModel):
    action: str                     # "on" | "off". No screen: X11 powers all
                                    # monitors together (agent/display.py).


class WindowIn(BaseModel):
    state: str                      # see browser.WINDOW_STATES
    screen: str | None = None


class ExtensionsIn(BaseModel):
    ids: list[str]                  # Web Store ids, never urls. See extensions.py.


class ExtensionOut(BaseModel):
    id: str                         # also its directory name
    name: str                       # from the manifest, for people


class ExtensionResult(BaseModel):
    id: str
    ok: bool
    name: str | None = None
    error: str | None = None


class ExtensionsOut(BaseModel):
    ok: bool
    pending_restart: bool           # installed, but not in the running browser
    installed: list[ExtensionOut]
    results: list[ExtensionResult] = []      # per-id, on install only


class ScreenResult(BaseModel):
    name: str
    ok: bool
    current_url: str | None = None
    error: str | None = None        # the same message the single-screen call 503s with


class NavigateOut(BaseModel):
    ok: bool                        # false = some screens took it and some did not
    current_url: str                # unchanged: the last screen that succeeded
    screens: list[ScreenResult] = []


class DisplayOut(BaseModel):
    ok: bool
    awake: bool


class MediaOut(BaseModel):
    ok: bool
    playing: bool
    muted: bool
    volume: int                     # 0-100, the element's own volume
    position: int                   # seconds
    duration: int                   # seconds; 0 for a live stream


class UploadOut(BaseModel):
    id: str
    url: str


class RegionIn(BaseModel):
    x: int = 0
    y: int = 0
    width: int = 0                  # 0 = to the right/bottom edge of the viewport
    height: int = 0


class ScreenshotIn(BaseModel):
    screen: str | None = None       # no "all": one request, one picture
    region: RegionIn | None = None
    format: str = "png"             # png | jpeg | webp
    quality: int = 80               # jpeg/webp only; png ignores it


class FieldOut(BaseModel):
    selector: str           # resolves again next call: an id or a name, never a path
    tag: str
    type: str = ""          # a "password" here is a login wall, in one field
    label: str = ""         # never the value — see browser._INSPECT_JS


class InspectOut(BaseModel):
    screen: str
    url: str
    title: str
    ready_state: str
    error_page: bool        # Chromium's own crash/network page, which /v1/status
    has_media: bool         # cannot see: it renders fine and answers 200
    scroll_y: int
    scroll_height: int
    fields: list[FieldOut] = []


class ActionIn(BaseModel):
    do: str                 # see browser.INPUT_ACTIONS
    x: int | None = None
    y: int | None = None
    selector: str | None = None     # preferred over x/y: survives a reflow
    text: str | None = None         # type
    key: str | None = None          # key
    modifiers: list[str] = []       # ctrl | alt | shift | meta
    ms: int | None = None           # wait
    # `from` is a Python keyword, so the wire name is an alias. drag takes
    # [x, y] pairs: two numbers, and no second nested model for a point.
    from_: list[int] | None = Field(default=None, alias="from")
    to: list[int] | None = None

    model_config = ConfigDict(populate_by_name=True)


class InputIn(BaseModel):
    screen: str | None = None
    actions: list[ActionIn]
    deadline_ms: int | None = None  # capped by [interact].deadline_ms


class ActionResult(BaseModel):
    do: str
    ok: bool
    took_ms: int
    x: int | None = None            # where a selector actually resolved to
    y: int | None = None
    error: str | None = None


class InputOut(BaseModel):
    ok: bool                        # false = it stopped partway; see results
    screen: str
    results: list[ActionResult]


class ScreenshotOut(BaseModel):
    image: str                      # base64, ready for a data: url
    format: str
    width: int                      # CSS pixels, and what you actually got --
    height: int                     # a clamped region returns its real size
    url: str                        # what the page says it is, beside the
    title: str                      # picture of it
    screen: str
    taken_at: float                 # unix seconds
    took_ms: int


class ScreenOut(BaseModel):
    name: str
    position: str
    current_url: str | None
    autoscroll: bool


class ScreenSettingIn(BaseModel):
    name: str
    home_url: str
    position: str = ""      # "" means "use what xrandr detected" — the reset path
    size: str = ""


class SettingsIn(BaseModel):
    screens: list[ScreenSettingIn]


class ScreenSettingOut(ScreenSettingIn):
    detected_position: str = ""     # what xrandr says right now, for placeholders
    detected_size: str = ""


class SettingsOut(BaseModel):
    screens: list[ScreenSettingOut]
    path: str               # where these persist, so the UI can say so
    note: str = ""          # saved, but the live window move didn't happen


class Status(BaseModel):
    # Always true, and useless: it means "this agent answered", which the 200
    # already told you. Kept because /v1 is frozen -- a client that does branch
    # on it would change behaviour the day we made it honest. `browser` and
    # `error` carry the news; README lists it as a frozen wart.
    up: bool
    current_url: str | None
    browser: str
    version: str
    awake: bool                     # whole display, not per screen
    screens: list[ScreenOut]
    # For a program rather than a person. `kind` and `supports` say which half of
    # this API exists on this box before you call it and collect a 501;
    # `started_at` changes when the agent restarts, which is how a poller learns
    # its autoscroll was dropped by the nightly restart timer.
    kind: str = ""
    supports: list[str] = []
    started_at: float = 0.0         # unix seconds
    # Why the browser is not there, when we know. Empty when it is fine, and
    # `browser` keeps its two original values -- a client reading `== "ok"` is
    # unaffected, and one that wants the reason has somewhere to read it. This
    # is the whole point of the agent outliving a failed launch: without it the
    # only diagnosis available is a journal on a box you cannot log into.
    error: str = ""


# --- autoscroll -------------------------------------------------------------
# A stop Event per screen, not an asyncio task: browser.py is blocking, routes
# already run in a threadpool, and an Event can be set from any thread --
# including _go(), which stops a scroll the moment the page changes.
_autoscroll: dict[str, threading.Event] = {}
# Guards the dict against the restart race -- see _autoscroll_start.
_autoscroll_lock = threading.Lock()


def _autoscroll_stop(name: str) -> None:
    with _autoscroll_lock:
        ev = _autoscroll.pop(name, None)
    if ev:
        ev.set()


def _autoscroll_start(cfg: dict, name: str, speed: int) -> None:
    _autoscroll_stop(name)
    stop = threading.Event()
    with _autoscroll_lock:
        _autoscroll[name] = stop

    def run() -> None:
        # The loop itself lives in browser.py so it can hold one CDP connection
        # for the whole run rather than opening one per tick.
        with contextlib.suppress(Exception):    # browser gone, screen closed
            browser.autoscroll(cfg, name, speed, stop)
        # Only if the entry is still *ours*. A second start installs its own
        # event under the same name, and popping unconditionally deleted that
        # one -- leaving the new run scrolling with nothing holding its stop
        # event, so `stop` popped nothing and the display went on scrolling
        # every page it was sent until the agent was restarted.
        with _autoscroll_lock:
            if _autoscroll.get(name) is stop:
                del _autoscroll[name]

    threading.Thread(target=run, daemon=True).start()


# Every route that touches the browser is `def`, not `async def`: browser.py is
# blocking websocket I/O, and on the event loop it starves uvicorn hard enough
# that the BiDi handshake fails. Sync routes get FastAPI's threadpool.
def _http(e: Exception) -> HTTPException:
    """The one place browser failures become status codes."""
    if isinstance(e, NotImplementedError):
        return HTTPException(501, str(e))
    if isinstance(e, ValueError):
        return HTTPException(422, str(e))
    return HTTPException(503, f"browser unreachable: {e}")


_BROWSER_ERRORS = (NotImplementedError, ValueError, OSError, RuntimeError)


def _fanout(screen: str | None, do) -> NavigateOut:
    """Run `do(s)` per targeted screen and report what each one did.

    One named screen keeps raising: one screen, one verdict, and every older
    client already handles that. `screen: "all"` must not — the loop is not
    atomic, so raising partway changes some monitors and tells the caller
    nothing about which. Collect instead. Every screen failing is still a 503.
    """
    picked = targets(app.state.cfg, screen)
    results: list[ScreenResult] = []
    last = None
    for s in picked:
        try:
            last = do(s)
            results.append(ScreenResult(name=s["name"], ok=True, current_url=last))
        except _BROWSER_ERRORS as e:
            if len(picked) == 1:
                raise _http(e)
            results.append(ScreenResult(name=s["name"], ok=False, error=str(e)))
    if last is None:
        raise _http(RuntimeError("; ".join(r.error or "" for r in results)))
    return NavigateOut(ok=all(r.ok for r in results), current_url=last,
                       screens=results)


def _navigate_one(s: dict, url: str) -> str:
    # Wake before navigating: pushing something to a sleeping display is how
    # you turn it back on. Covers navigate, home, reload and upload.
    display.touch(s, url)
    # Any navigation ends an autoscroll on that screen, or the loop keeps
    # scrolling whatever page lands next -- a haunted display, from the UI.
    _autoscroll_stop(s["name"])
    return browser.navigate(app.state.cfg, url, s["name"])


def _go(url: str, screen: str | None = None) -> NavigateOut:
    return _fanout(screen, lambda s: _navigate_one(s, url))


@app.post("/v1/navigate", response_model=NavigateOut, dependencies=[Depends(auth)])
def navigate(body: NavigateIn) -> NavigateOut:
    return _go(str(body.url), body.screen)


@app.post("/v1/home", response_model=NavigateOut, dependencies=[Depends(auth)])
def home(body: ScreenIn | None = None) -> NavigateOut:
    # Each screen has its own home, so the url is per screen, not per request.
    return _fanout(body.screen if body else None,
                   lambda s: _navigate_one(s, s["home_url"]))


@app.post("/v1/reload", response_model=NavigateOut, dependencies=[Depends(auth)])
def reload(body: ScreenIn | None = None) -> NavigateOut:
    # ponytail: re-navigate rather than a real reload — same result for a display,
    # and one code path. Use BiDi browsingContext.reload / CDP Page.reload if a
    # page ever needs its POST state kept.
    def one(s: dict) -> str:
        return _navigate_one(s, browser.current_url(app.state.cfg, s["name"]))

    return _fanout(body.screen if body else None, one)


@app.post("/v1/window", response_model=NavigateOut, dependencies=[Depends(auth)])
def window(body: WindowIn) -> NavigateOut:
    # No display.touch(): setting a window aside is not "show me something", and
    # waking the panel in order to minimize a window is backwards.
    return _fanout(body.screen,
                   lambda s: browser.window(app.state.cfg, s, body.state))


@app.post("/v1/scroll", response_model=NavigateOut, dependencies=[Depends(auth)])
def scroll(body: ScrollIn) -> NavigateOut:
    def one(s: dict) -> str:
        display.touch(s)  # reading a long PDF is activity, even without a navigate
        browser.scroll(app.state.cfg, s["name"], dy=body.dy, to=body.to)
        return browser.current_url(app.state.cfg, s["name"])

    return _fanout(body.screen, one)


@app.post("/v1/autoscroll", response_model=NavigateOut, dependencies=[Depends(auth)])
def autoscroll(body: AutoScrollIn) -> NavigateOut:
    """Start or stop a slow continuous scroll.

    **Wart, frozen:** the reply reuses NavigateOut but puts a *screen name* in
    `current_url` -- the last screen's, with `screens` empty. Wrong, and it
    stays wrong: /v1 is frozen and a client parsing it would change behaviour
    the day we fixed it. Read the 200, not the body. README lists it.
    """
    if body.action not in ("start", "stop"):
        raise HTTPException(422, "action must be 'start' or 'stop'")
    cfg = app.state.cfg
    # Checked here rather than left to browser.autoscroll: _autoscroll_start runs
    # it on a thread that suppresses everything, so a 501 would never come back.
    # Off the same SUPPORTS table the browser guard reads.
    if body.action == "start" and "autoscroll" not in browser.supports(cfg):
        raise HTTPException(501, "autoscroll needs CDP; use chromium or edge")
    out = None
    for s in targets(cfg, body.screen):
        if body.action == "start":
            _autoscroll_start(cfg, s["name"], body.speed)
        else:
            _autoscroll_stop(s["name"])
        out = NavigateOut(ok=True, current_url=s["name"])
    return out


@app.post("/v1/media", response_model=MediaOut, dependencies=[Depends(auth)])
def media(body: MediaIn) -> MediaOut:
    """Play, pause, seek, mute or set the volume of whatever the screen is
    showing. `action: "state"` just reports, so a controller can poll it."""
    cfg = app.state.cfg
    out = None
    for s in targets(cfg, body.screen):
        # Not on "state": a controller left open polling this would keep the room
        # lit all night, which is exactly what the idle timer exists to prevent.
        if body.action != "state":
            display.touch(s)
        try:
            state = browser.media(cfg, s["name"], body.action, body.value)
        except _BROWSER_ERRORS as e:
            # ponytail: raises on the first bad screen rather than collecting
            # like _fanout -- MediaOut carries a player state, not a url, so
            # per-screen results need a second model. Add one when a wall really
            # does play different things on different monitors.
            raise _http(e)
        if state is not None:
            out = MediaOut(ok=True, **state)
    # "all" over a wall where only one screen has a video is a success, not a
    # 404 — the request did what it could. Nothing anywhere is the error.
    if out is None:
        raise HTTPException(404, f"nothing playing on {body.screen or 'the display'}")
    return out


@app.post("/v1/screenshot", response_model=ScreenshotOut, dependencies=[Depends(auth)])
def screenshot(body: ScreenshotIn) -> ScreenshotOut:
    """What one screen is *actually* showing.

    Every other route reports the url it was *given*, so a redirect, a login
    wall, a consent banner and a crashed tab all look like success. This is the
    read-back. POST because the body carries a region, but still a read.

    No `display.touch()`: waking the panel to photograph it would let a poller
    light the room all night (same as `/v1/window` and `media action=state`).
    The page renders whether or not the monitor is powered, so this works on a
    sleeping display.
    """
    cfg = app.state.cfg
    # screen_of, not targets(): "all" would have to return a list of images, and
    # nothing has asked for that. One request, one picture, and an unknown name
    # still 404s the way it does everywhere else.
    s = screen_of(cfg, body.screen)
    started = time.monotonic()
    try:
        shot = browser.screenshot(cfg, s["name"],
                                  region=body.region.model_dump() if body.region else None,
                                  format=body.format, quality=body.quality)
    except _BROWSER_ERRORS as e:
        raise _http(e)
    return ScreenshotOut(screen=s["name"], taken_at=time.time(),
                         took_ms=int((time.monotonic() - started) * 1000), **shot)


@app.get("/v1/inspect", response_model=InspectOut, dependencies=[Depends(auth)])
def inspect(screen: str | None = None) -> InspectOut:
    """What the page says about itself: title, ready state, scroll, form fields.

    The machine-readable half of `/v1/screenshot`. `error_page` is the one a
    poller most needs: Chromium's own crash and network pages render perfectly
    and answer `/v1/status` with a 200.

    No field *values*, ever — naming a password box is how a caller knows where
    to type; returning what is in it would make this a credential leak. A read,
    so no `display.touch()`.
    """
    cfg = app.state.cfg
    s = screen_of(cfg, screen)
    try:
        return InspectOut(screen=s["name"], **browser.inspect(cfg, s["name"]))
    except _BROWSER_ERRORS as e:
        raise _http(e)


@app.post("/v1/input", response_model=InputOut, dependencies=[Depends(auth)])
def send_input(body: InputIn) -> InputOut:
    """Click, drag, type and press keys on one screen, in order.

    For the failure this box cannot otherwise recover from: no keyboard, so an
    expired SSO login or a consent wall is a page nobody can get past (PLAN.md
    §6). Everything goes into one browser target's renderer — it cannot reach
    the window manager, the desktop, or any other application.

    **Off unless `[interact] enabled = true`**: the only route that acts *as*
    whoever the kiosk is logged in as. Disabled, it is absent from `supports`
    and 501s, exactly like a capability the browser lacks.

    One request per sequence, not per verb — five requests is five websockets
    and five chances to interleave. Stops at the first failure: a click that
    missed would otherwise be followed by a password typed into whatever does
    have focus.
    """
    cfg = app.state.cfg
    if not browser.interactive(cfg):
        raise HTTPException(501, "input is disabled; set [interact] enabled = true "
                                 "in config.toml and restart the agent")
    limits = cfg["interact"]
    if len(body.actions) > limits["max_actions"]:
        raise HTTPException(422, f"at most {limits['max_actions']} actions, "
                                 f"got {len(body.actions)}")
    s = screen_of(cfg, body.screen)
    # The caller may ask for less time, never more: a deadline is what stops one
    # request holding a threadpool thread while a page never settles.
    ms = min(body.deadline_ms or limits["deadline_ms"], limits["deadline_ms"])

    actions = [a.model_dump(exclude_none=True, by_alias=True) for a in body.actions]
    # Typing is what this route is for, and a password is what it will mostly
    # type. The audit line records that text was entered and how much, never
    # what. Everything else is safe to name in full.
    log.info("input on %s: %s", s["name"],
             ", ".join(f"type({len(a['text'])} chars)" if a["do"] == "type"
                       else a["do"] for a in actions))
    display.touch(s)        # acting on a screen is activity, and you want to see it
    try:
        results = browser.input(cfg, s["name"], actions,
                                deadline=time.monotonic() + ms / 1000)
    except _BROWSER_ERRORS as e:
        raise _http(e)      # a malformed list is a 422; nothing has run
    out = [ActionResult(**r) for r in results]
    if not all(r.ok for r in out):
        log.warning("input on %s stopped at action %d: %s", s["name"],
                    len(out) - 1, out[-1].error)
    return InputOut(ok=all(r.ok for r in out), screen=s["name"], results=out)


@app.post("/v1/display", response_model=DisplayOut, dependencies=[Depends(auth)])
def display_power(body: DisplayIn) -> DisplayOut:
    """Turn the monitors off when you leave, or back on. Every other /v1 route
    already wakes them, so this exists for "off" — "on" is just the way back if
    you hit it by mistake."""
    if body.action not in ("on", "off"):
        raise HTTPException(422, "action must be 'on' or 'off'")
    if body.action == "off":
        display.power(False)
    else:
        # Reset every idle clock too, or the next tick finds them all long idle
        # and puts the display straight back to sleep.
        for s in app.state.cfg["screens"]:
            display.touch(s)
    return DisplayOut(ok=True, awake=display.awake())


@app.get("/v1/screens", response_model=list[ScreenOut], dependencies=[Depends(auth)])
def screens() -> list[ScreenOut]:
    return [_screen_out(s) for s in app.state.cfg["screens"]]


# --- extensions -------------------------------------------------------------
# Ad blockers, mostly. The kiosk has no UI to install one through and this
# Chromium ignores ExtensionInstallForcelist (deploy/pi/README.md §10), so the
# agent fetches the CRX itself. The only route that writes executable code onto
# the box, so it takes *ids* and never a url — extensions.py, PLAN.md §11.

def _ext_dir() -> str:
    if "extensions" not in browser.supports(app.state.cfg):
        raise HTTPException(501, "extensions need CDP; use chromium or edge")
    return app.state.cfg["browser"].get("extensions_dir", "")


def _extensions_out(results: list[ExtensionResult] | None = None) -> ExtensionsOut:
    d = _ext_dir()
    return ExtensionsOut(
        ok=all(r.ok for r in results) if results else True,
        # --load-extension is a launch flag: nothing here is live until the
        # browser restarts, and saying so is the whole point of this field.
        pending_restart=extensions.pending(d, browser._loaded),
        installed=[ExtensionOut(id=Path(p).name, name=extensions.display_name(p))
                   for p in extensions.scan(d)],
        results=results or [])


@app.get("/v1/extensions", response_model=ExtensionsOut, dependencies=[Depends(auth)])
def get_extensions() -> ExtensionsOut:
    return _extensions_out()


@app.post("/v1/extensions", response_model=ExtensionsOut, dependencies=[Depends(auth)])
def install_extensions(body: ExtensionsIn) -> ExtensionsOut:
    d = _ext_dir()
    # Every id checked before anything is fetched: a typo is total, so the
    # request fails rather than half-installing. Failures after that are per-id
    # -- raising partway would install some and report none (_fanout).
    for i in body.ids:
        if not extensions.ID_RE.match(i or ""):
            raise HTTPException(422, f"not an extension id: {i!r}")
    if not d:
        raise HTTPException(422, "no extensions_dir set in config.toml")

    results = []
    for i in body.ids:
        try:
            results.append(ExtensionResult(id=i, ok=True,
                                           name=extensions.install(d, i)))
        except (OSError, ValueError, extensions.TooBig) as e:
            results.append(ExtensionResult(id=i, ok=False, error=str(e)))
    return _extensions_out(results)


@app.delete("/v1/extensions/{name}", response_model=ExtensionsOut,
            dependencies=[Depends(auth)])
def remove_extension(name: str) -> ExtensionsOut:
    try:
        extensions.remove(_ext_dir(), name)
    except KeyError:
        raise HTTPException(404, f"no extension {name!r} installed")
    return _extensions_out()


# --- settings ---------------------------------------------------------------
# The screens editor (PLAN.md §7, v1.1.0). Only what is safe to change while the
# agent runs; config.toml is never made agent-writable.

def _settings_out(note: str = "") -> SettingsOut:
    found = display.detect()
    return SettingsOut(
        path=str(settings.path()), note=note,
        screens=[ScreenSettingOut(
            name=s["name"], home_url=s["home_url"],
            position=s["position"], size=s["size"],
            detected_position=found[i]["position"] if i < len(found) else "",
            detected_size=found[i]["size"] if i < len(found) else "")
            for i, s in enumerate(app.state.cfg["screens"])])


@app.get("/v1/settings", response_model=SettingsOut, dependencies=[Depends(auth)])
def get_settings() -> SettingsOut:
    return _settings_out()


@app.put("/v1/settings", response_model=SettingsOut, dependencies=[Depends(auth)])
def put_settings(body: SettingsIn) -> SettingsOut:
    """Save the screens editor, then apply it live.

    Everything is validated *before* anything is written: a bad position that
    reaches settings.json breaks the next boot, and the box has no keyboard.
    """
    cfg = app.state.cfg
    if len(body.screens) != len(cfg["screens"]):
        raise HTTPException(422, f"expected {len(cfg['screens'])} screens, "
                                 f"got {len(body.screens)}")
    names = [s.name.strip() for s in body.screens]
    if not all(names):
        raise HTTPException(422, "screen names cannot be empty")
    if len(set(names)) != len(names):
        raise HTTPException(422, f"duplicate screen names {names}")
    for s in body.screens:
        if not s.home_url.startswith(("http://", "https://")):
            raise HTTPException(422, f"{s.home_url!r}: home_url must be http or https")
        # browser._pair is the one place that knows these formats and it already
        # names the offending value; a second regex here would only drift.
        try:
            if s.position.strip():
                browser._pair(s.position.strip(), ",", "position")
            if s.size.strip():
                browser._pair(s.size.strip(), "x", "size")
        except RuntimeError as e:
            raise HTTPException(422, str(e))

    before = [(s["position"], s["size"]) for s in cfg["screens"]]
    rows = [{"name": n, "home_url": s.home_url.strip(),
             "position": s.position.strip(), "size": s.size.strip()}
            for n, s in zip(names, body.screens)]
    try:
        # Merged, not replaced: the editor only ever sees the monitors detected
        # right now, and a save with one unplugged must not delete the other
        # screen's saved name and home_url. See settings.merge_screens.
        settings.save(settings.merge_screens(rows))
    except (OSError, RuntimeError) as e:      # unwritable dir, or no resolvable home
        raise HTTPException(500, f"cannot save settings: {e}")

    # Through load_config(), not field by field, so a save lands exactly where a
    # restart would -- no second implementation of the ?screen= stamping. In
    # place, not reassigned: display.watch() closed over this dict.
    swap_config(cfg, load_config())

    # The live half, and the only reason this beats editing a file: the window
    # moves while you watch. It must not fail the request -- the settings are
    # already saved, and a dead browser is not a bad save.
    note = ""
    for i, s in enumerate(cfg["screens"]):
        if (s["position"], s["size"]) == before[i] or not s["position"]:
            continue
        try:
            browser.place(cfg, s)
        except (NotImplementedError, OSError, RuntimeError) as e:
            note = f"saved, but {s['name']} was not moved: {e}"
    return _settings_out(note)


def _screen_out(s: dict) -> ScreenOut:
    try:
        url = browser.current_url(app.state.cfg, s["name"])
    except _BROWSER_ERRORS:     # NotImplementedError: firefox, screens 2 and up
        url = None
    return ScreenOut(name=s["name"], position=s["position"], current_url=url,
                     autoscroll=s["name"] in _autoscroll)


@app.post("/v1/upload", response_model=UploadOut, dependencies=[Depends(auth)])
def upload(request: Request, file: UploadFile,
           screen: str | None = Form(None),
           navigate: bool = Form(True)) -> UploadOut:
    """Store a file and show it. `navigate=false` stages it instead: you get the
    url back without anything appearing on the wall, so a program can prepare
    content and choose when it goes up."""
    try:
        file_id = storage.save(app.state.cfg, file.filename,
                               iter(lambda: file.file.read(1 << 20), b""))
    except storage.BadType as e:
        raise HTTPException(415, str(e))
    except storage.TooBig as e:
        raise HTTPException(413, str(e))

    # The same address the uploader used, not loopback: the Pi binds the tailnet
    # interface only (PLAN.md §10), so 127.0.0.1 would 404 on the display.
    if navigate:
        _go(str(request.url_for("serve_file", file_id=file_id)), screen)
    return UploadOut(id=file_id, url=f"/files/{file_id}")


# Unauthenticated: the kiosk browser fetches this and cannot send a bearer
# header. The 16-char random id is the capability — ids are never listed.
@app.get("/files/{file_id}", include_in_schema=False)
def serve_file(file_id: str) -> FileResponse:
    try:
        p = storage.path(app.state.cfg, file_id)
    except KeyError:
        raise HTTPException(404, "no such file")
    # nosniff: we serve the type the extension claims, never the client's --
    # this route is unauthenticated and same-origin with the web UI holding the
    # token, so a .txt talked into rendering as HTML would run there.
    #
    # CSP sandbox is defence in depth for whatever lands in storage.TYPES later:
    # an opaque origin cannot reach that localStorage. `allow-scripts` because a
    # bare `sandbox` is *believed* to render Chromium's PDF viewer blank -- not
    # measured, so do not treat this header as the boundary. The boundary is
    # test_nothing_in_types_can_execute: nothing served here is script-bearing.
    return FileResponse(p, media_type=storage.media_type(file_id),
                        content_disposition_type="inline",
                        headers={"X-Content-Type-Options": "nosniff",
                                 "Content-Security-Policy": "sandbox allow-scripts"})


@app.get("/v1/status", response_model=Status, dependencies=[Depends(auth)])
def status() -> Status:
    cfg = app.state.cfg
    # A failing launch is the better explanation, so it wins: the socket error
    # only says "connection refused" on a port nothing ever opened.
    error = getattr(app.state, "launch_error", "")
    try:
        url, state = browser.current_url(cfg), "ok"
    except _BROWSER_ERRORS as e:
        url, state = None, "down"
        error = error or f"{type(e).__name__}: {e}"
    # current_url stays the first screen's, so a pre-multi-monitor client that
    # reads it keeps working unchanged.
    return Status(up=True, current_url=url, browser=state,
                  version=os.getenv("ROOM_VERSION", "dev"), awake=display.awake(),
                  screens=[_screen_out(s) for s in cfg["screens"]],
                  kind=cfg["browser"]["kind"], supports=browser.supports(cfg),
                  started_at=getattr(app.state, "started_at", 0.0), error=error)


# Unauthenticated on purpose: you need the page before you can type the token.
# It ships no secrets — every /v1 call it makes carries the bearer header.
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent.parent / "web" / "index.html")


# Also unauthenticated, for the same reason /files/{id} is: the kiosk browser
# fetches this and cannot send a bearer header.
@app.get("/home", include_in_schema=False)
def home_page() -> FileResponse:
    return FileResponse(Path(__file__).parent.parent / "web" / "home.html")


@app.get("/home-status", include_in_schema=False)
def home_status() -> dict:
    """What the home screen may display. Deliberately *not* /v1/status: this is
    unauthenticated, so it reports the **host** of what a screen is showing and
    never the full url -- a link to a private document is worth more than the
    convenience of seeing it on an idle screen."""
    out = []
    for s in app.state.cfg["screens"]:
        try:
            url = browser.current_url(app.state.cfg, s["name"])
        except _BROWSER_ERRORS:
            url = None
        host = urlparse(url).hostname if url else None
        # Its own home page is idle, not "showing" -- otherwise every screen
        # reports the agent's own host forever.
        if not host or urlparse(url).path.startswith("/home") \
                or (url or "").startswith(("about:", "data:")):
            host = None
        out.append({"name": s["name"], "showing": host})
    return {"name": app.state.cfg["screens"][0]["name"],
            "version": os.getenv("ROOM_VERSION", "dev"), "screens": out}
