"""Room display agent — frozen /v1 contract (PLAN.md §5)."""

import contextlib
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
from pydantic import AnyHttpUrl, BaseModel

from . import browser, display, settings, storage

DEFAULTS = {
    "kind": "firefox", "path": "", "profile_dir": "", "autolaunch": True,
    "debug_port": 9222, "disk_cache_mb": 100,
}
UPLOAD_DEFAULTS = {"dir": "", "max_mb": 25, "keep": 5}
# Where to bind. Empty host = loopback, which is the safe default anywhere that
# is not the Pi; the Pi's config sets its tailnet address (PLAN.md §10 — never
# 0.0.0.0). This lives here rather than only in the systemd unit so that
# something other than systemd can start the agent.
SERVER_DEFAULTS = {"host": "127.0.0.1", "port": 8080}
SCREEN_DEFAULTS = {"name": "", "position": "", "size": "", "home_url": ""}


def load_config(path: str | os.PathLike | None = None) -> dict:
    path = Path(path or os.getenv("ROOM_CONFIG") or Path(__file__).parent / "config.toml")
    # utf-8-sig: Windows editors and PowerShell write a BOM that tomllib chokes on.
    cfg = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    cfg["browser"] = DEFAULTS | cfg.get("browser", {})
    cfg["upload"] = UPLOAD_DEFAULTS | cfg.get("upload", {})
    cfg["server"] = SERVER_DEFAULTS | cfg.get("server", {})
    cfg["display"] = display.DEFAULTS | cfg.get("display", {})
    if not cfg["browser"]["profile_dir"]:
        cfg["browser"]["profile_dir"] = str(path.parent / "profile")
    if not cfg["upload"]["dir"]:
        cfg["upload"]["dir"] = str(path.parent / "uploads")
    if not cfg.get("token"):
        raise RuntimeError(f"{path}: token is required")

    # No [[screen]] blocks -> ask X what monitors exist, so a fresh install drives
    # every connected screen without anyone editing a config file. Explicit blocks
    # always win, so this changes nothing for a config that has them. Nothing
    # detected (Windows, no DISPLAY) -> one screen called "main", exactly the
    # single-monitor behaviour every existing config already has.
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
        s["home_url"] = s["home_url"] or cfg.get("home_url", "about:blank")
        # The idle page names the monitor it is on, and the url is the only way
        # it can know: every window shares one profile and one debug port, so
        # there is nothing else to tell them apart client-side.
        # Match on the path: a url ending "?x=1" still points at /home.
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
    app.state.cfg = cfg = load_config()
    # Reported by /v1/status. A controller polling us cannot otherwise tell "your
    # autoscroll is still running" from "the 04:00 restart timer fired and threw
    # it away" — autoscroll state is deliberately not persisted.
    app.state.started_at = time.time()
    # We own the kiosk only if we started it — a no-input box must not be left
    # with an orphaned fullscreen window after the agent stops.
    # ROOM_SELFCHECK: `python -m agent selfcheck` boots this app in-process to
    # prove a new release can start, while the live kiosk is still running the
    # old one. It must never launch a second browser onto that port or screen.
    launch = cfg["browser"]["autolaunch"] and not os.getenv("ROOM_SELFCHECK")
    proc = browser.launch(cfg) if launch else None
    watching = None
    if proc:
        # Only when we own the kiosk: selfcheck boots this app beside a running
        # instance, and must not reach out and blank the real monitors.
        # claim() itself runs on the thread below, off the startup path: lifespan
        # blocks the port from binding, and update.sh rolls the release back if
        # /v1/status doesn't answer within 30s of the restart.
        watching = display.watch(cfg)
        threading.Thread(target=_home_when_ready, args=(cfg,), daemon=True).start()
    yield
    for name in list(_autoscroll):
        _autoscroll_stop(name)
    if watching:
        watching.set()
    if proc:
        _save_shown(cfg)         # while the browser can still be asked
        browser.stop(cfg, proc)  # ours: take the whole tree down with us
    else:
        browser.close()  # not ours: just release the session and leave it running


def _home_when_ready(cfg: dict) -> None:
    """Send each screen to its home page once we are actually serving it.

    uvicorn runs this lifespan *before* it binds the socket, so a kiosk pointed
    at the agent's own /home during launch renders "can't be reached" and stays
    there. Wait for the port, then navigate.
    """
    display.claim()          # take DPMS off the session before anything can blank
    urls = [s["home_url"] for s in cfg["screens"]]
    probe = next((u for u in urls if u.startswith(("http://", "https://"))), None)
    if not probe:
        return                                  # about:blank et al: nothing to wait for
    for _ in range(60):
        try:
            httpx.get(probe, timeout=2)
            break
        except httpx.HTTPError:
            time.sleep(1)
    else:
        return
    shown = {}
    with contextlib.suppress(Exception):    # a mangled last.json costs the
        shown = _restorable(cfg)            # restore, never the boot
    for s in cfg["screens"]:
        with contextlib.suppress(OSError, RuntimeError):
            browser.navigate(cfg, shown.get(s["name"]) or s["home_url"], s["name"])


def _save_shown(cfg: dict) -> None:
    """Record what each screen is showing, for the next start to put back.

    Once, at shutdown, rather than on every navigate: on the Pi this file is on
    the SD card, and not writing to that is most of what Phase 6 is about.
    """
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

    The nightly restart (deploy/pi/room-display-restart.timer) is there to stop
    Chromium running the Pi out of memory, but it must not quietly clear the
    wall: something put up at 5pm should still be up in the morning. Something
    from last week should not — that is what the window is for.
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


def auth(app_cfg: HTTPAuthorizationCredentials = Depends(_bearer)) -> None:
    if not secrets.compare_digest(app_cfg.credentials, app.state.cfg["token"]):
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


# --- autoscroll -------------------------------------------------------------
# A stop Event per screen, not an asyncio task: browser.py is blocking, the
# routes already run in a threadpool, and an Event can be set from any thread --
# including from _go(), which has to stop a scroll the moment the page changes.
_autoscroll: dict[str, threading.Event] = {}


def _autoscroll_stop(name: str) -> None:
    ev = _autoscroll.pop(name, None)
    if ev:
        ev.set()


def _autoscroll_start(cfg: dict, name: str, speed: int) -> None:
    _autoscroll_stop(name)
    stop = _autoscroll[name] = threading.Event()

    def run() -> None:
        # The loop itself lives in browser.py so it can hold one CDP connection
        # for the whole run rather than opening one per tick.
        with contextlib.suppress(Exception):    # browser gone, screen closed
            browser.autoscroll(cfg, name, speed, stop)
        _autoscroll.pop(name, None)

    threading.Thread(target=run, daemon=True).start()


# Every route that touches the browser is `def`, not `async def`: browser.py is
# blocking websocket I/O, and on the event loop it starves uvicorn hard enough
# that the BiDi handshake fails. Sync routes get FastAPI's threadpool.
def _http(e: Exception) -> HTTPException:
    """The one place browser failures become status codes. Was repeated inline in
    every route; a fan-out has to raise the same codes from a helper."""
    if isinstance(e, NotImplementedError):
        return HTTPException(501, str(e))
    if isinstance(e, ValueError):
        return HTTPException(422, str(e))
    return HTTPException(503, f"browser unreachable: {e}")


_BROWSER_ERRORS = (NotImplementedError, ValueError, OSError, RuntimeError)


def _fanout(screen: str | None, do) -> NavigateOut:
    """Run `do(s)` per targeted screen and report what each one did.

    One named screen keeps raising: one screen, one verdict, and every client
    written before this already handles that. `screen: "all"` must not — the loop
    is not atomic, so raising partway leaves some monitors changed and tells the
    caller nothing about which ones. Collect instead, and let `screens` carry the
    per-screen truth that a single `current_url` cannot.

    Every screen failing is still a 503: the request accomplished nothing, and
    that is what it has always meant.
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
    # Wake before navigating: pushing something to a sleeping display is how you
    # turn it back on. This covers navigate, home, reload and upload.
    display.touch(s, url)
    # Any navigation ends an autoscroll on that screen. Without this the loop
    # keeps scrolling whatever page lands next, which looks like a haunted
    # display and is impossible to guess from the UI.
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


@app.post("/v1/scroll", response_model=NavigateOut, dependencies=[Depends(auth)])
def scroll(body: ScrollIn) -> NavigateOut:
    def one(s: dict) -> str:
        display.touch(s)  # reading a long PDF is activity, even without a navigate
        browser.scroll(app.state.cfg, s["name"], dy=body.dy, to=body.to)
        return browser.current_url(app.state.cfg, s["name"])

    return _fanout(body.screen, one)


@app.post("/v1/autoscroll", response_model=NavigateOut, dependencies=[Depends(auth)])
def autoscroll(body: AutoScrollIn) -> NavigateOut:
    if body.action not in ("start", "stop"):
        raise HTTPException(422, "action must be 'start' or 'stop'")
    cfg = app.state.cfg
    if body.action == "start" and cfg["browser"]["kind"] == "firefox":
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
            # ponytail: media keeps raising on the first bad screen rather than
            # collecting like _fanout. Its "all" already means "whichever screen
            # had media", and MediaOut carries a player state, not a url, so
            # per-screen results need a second result model. Add one when a wall
            # really does play different things on different monitors.
            raise _http(e)
        if state is not None:
            out = MediaOut(ok=True, **state)
    # "all" over a wall where only one screen has a video is a success, not a
    # 404 — the request did what it could. Nothing anywhere is the error.
    if out is None:
        raise HTTPException(404, f"nothing playing on {body.screen or 'the display'}")
    return out


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


# --- settings ---------------------------------------------------------------
# The screens editor (PLAN.md §7, v1.1.0). Only what is safe to change while the
# agent runs: config.toml stays the source for the token and the install-time
# paths, and is never made agent-writable.

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
    data = {"screens": [{"name": n, "home_url": s.home_url.strip(),
                         "position": s.position.strip(), "size": s.size.strip()}
                        for n, s in zip(names, body.screens)]}
    try:
        settings.save(data)
    except (OSError, RuntimeError) as e:      # unwritable dir, or no resolvable home
        raise HTTPException(500, f"cannot save settings: {e}")

    # Reload through load_config() rather than patching cfg field by field, so a
    # save lands exactly where a restart would — one code path, no second
    # implementation of the ?screen= stamping. Mutated in place, *not*
    # reassigned: display.watch() closed over this dict at startup and would
    # otherwise read a stale config forever.
    #
    # Built first, then swapped in back to back. Every browser route is `def`,
    # so it runs on the threadpool: clearing before load_config() left cfg empty
    # across a file read and an xrandr subprocess, and any concurrent request
    # reading cfg["screens"] in that window got a KeyError.
    fresh = load_config()
    cfg.clear()
    cfg.update(fresh)

    # The live half, and the only reason this beats editing a file: the window
    # moves while you watch. It must not fail the request -- the settings are
    # already saved, and a dead browser or a Firefox dev box is not a bad save.
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

    # Send the kiosk to the same address the uploader used. Not loopback: on the
    # Pi we bind the tailnet interface only (PLAN.md §10), so 127.0.0.1 is not
    # listening and every dropped file would 404 on the display.
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
    # nosniff: we serve the type the extension claims, never the client's. This
    # route is unauthenticated and same-origin with the web UI that holds the
    # token, so a .txt talked into rendering as HTML would run there.
    return FileResponse(p, media_type=storage.media_type(file_id),
                        content_disposition_type="inline",
                        headers={"X-Content-Type-Options": "nosniff"})


@app.get("/v1/status", response_model=Status, dependencies=[Depends(auth)])
def status() -> Status:
    cfg = app.state.cfg
    try:
        url, state = browser.current_url(cfg), "ok"
    except _BROWSER_ERRORS:
        url, state = None, "down"
    # current_url stays the first screen's, so a pre-multi-monitor client that
    # reads it keeps working unchanged.
    return Status(up=True, current_url=url, browser=state,
                  version=os.getenv("ROOM_VERSION", "dev"), awake=display.awake(),
                  screens=[_screen_out(s) for s in cfg["screens"]],
                  kind=cfg["browser"]["kind"], supports=browser.supports(cfg),
                  started_at=getattr(app.state, "started_at", 0.0))


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
    """What the home screen may display. Deliberately *not* /v1/status.

    Anyone who can reach the agent can read this, so it reports the **host** of
    what a screen is showing, never the full url — a link to a private document
    is worth more than the convenience of seeing it on the idle screen.
    """
    out = []
    for s in app.state.cfg["screens"]:
        try:
            url = browser.current_url(app.state.cfg, s["name"])
        except _BROWSER_ERRORS:
            url = None
        host = urlparse(url).hostname if url else None
        # Sitting on its own home page is idle, not "showing" something. Without
        # this every screen reports the agent's own host forever.
        if not host or urlparse(url).path.startswith("/home") \
                or (url or "").startswith(("about:", "data:")):
            host = None
        out.append({"name": s["name"], "showing": host})
    return {"name": app.state.cfg["screens"][0]["name"],
            "version": os.getenv("ROOM_VERSION", "dev"), "screens": out}
