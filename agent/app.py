"""Room display agent — frozen /v1 contract (PLAN.md §5)."""

import contextlib
import os
import secrets
import threading
import time
import tomllib
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AnyHttpUrl, BaseModel

from . import browser, display, storage

DEFAULTS = {
    "kind": "firefox", "path": "", "profile_dir": "", "autolaunch": True,
    "debug_port": 9222, "disk_cache_mb": 100,
}
UPLOAD_DEFAULTS = {"dir": "", "max_mb": 25, "keep": 20}
SCREEN_DEFAULTS = {"name": "", "position": "", "size": "", "home_url": ""}


def load_config(path: str | os.PathLike | None = None) -> dict:
    path = Path(path or os.getenv("ROOM_CONFIG") or Path(__file__).parent / "config.toml")
    # utf-8-sig: Windows editors and PowerShell write a BOM that tomllib chokes on.
    cfg = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    cfg["browser"] = DEFAULTS | cfg.get("browser", {})
    cfg["upload"] = UPLOAD_DEFAULTS | cfg.get("upload", {})
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
    for i, s in enumerate(cfg["screens"]):
        s["name"] = s["name"] or ("main" if i == 0 else f"screen{i + 1}")
        s["home_url"] = s["home_url"] or cfg.get("home_url", "about:blank")
        # The idle page names the monitor it is on, and the url is the only way
        # it can know: every window shares one profile and one debug port, so
        # there is nothing else to tell them apart client-side.
        # Match on the path: a url ending "?x=1" still points at /home.
        if urlparse(s["home_url"]).path.rstrip("/").endswith("/home") \
                and "screen=" not in s["home_url"]:
            sep = "&" if "?" in s["home_url"] else "?"
            s["home_url"] += f"{sep}screen={quote(s['name'])}"
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
        display.claim()
        watching = display.watch(cfg)
        threading.Thread(target=_home_when_ready, args=(cfg,), daemon=True).start()
    yield
    for name in list(_autoscroll):
        _autoscroll_stop(name)
    if watching:
        watching.set()
    if proc:
        browser.stop(cfg, proc)  # ours: take the whole tree down with us
    else:
        browser.close()  # not ours: just release the session and leave it running


def _home_when_ready(cfg: dict) -> None:
    """Send each screen to its home page once we are actually serving it.

    uvicorn runs this lifespan *before* it binds the socket, so a kiosk pointed
    at the agent's own /home during launch renders "can't be reached" and stays
    there. Wait for the port, then navigate.
    """
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
    for s in cfg["screens"]:
        with contextlib.suppress(OSError, RuntimeError):
            browser.navigate(cfg, s["home_url"], s["name"])


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


class DisplayIn(BaseModel):
    action: str                     # "on" | "off". No screen: X11 powers all
                                    # monitors together (agent/display.py).


class NavigateOut(BaseModel):
    ok: bool
    current_url: str


class DisplayOut(BaseModel):
    ok: bool
    awake: bool


class UploadOut(BaseModel):
    id: str
    url: str


class ScreenOut(BaseModel):
    name: str
    position: str
    current_url: str | None
    autoscroll: bool


class Status(BaseModel):
    up: bool
    current_url: str | None
    browser: str
    version: str
    awake: bool                     # whole display, not per screen
    screens: list[ScreenOut]


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
        while not stop.wait(0.1):
            try:
                browser.scroll(cfg, name, dy=speed)
            except Exception:       # browser gone, screen closed: just stop
                break
        _autoscroll.pop(name, None)

    threading.Thread(target=run, daemon=True).start()


# Every route that touches the browser is `def`, not `async def`: browser.py is
# blocking websocket I/O, and on the event loop it starves uvicorn hard enough
# that the BiDi handshake fails. Sync routes get FastAPI's threadpool.
def _go(url: str, screen: str | None = None) -> NavigateOut:
    cfg = app.state.cfg
    last = url
    try:
        for s in targets(cfg, screen):
            # Wake before navigating: pushing something to a sleeping display is
            # how you turn it back on. This one line covers navigate, home,
            # reload and upload.
            display.touch(s, url)
            # Any navigation ends an autoscroll on that screen. Without this the
            # loop keeps scrolling whatever page lands next, which looks like a
            # haunted display and is impossible to guess from the UI.
            _autoscroll_stop(s["name"])
            last = browser.navigate(cfg, url, s["name"])
    except (OSError, RuntimeError) as e:
        raise HTTPException(503, f"browser unreachable: {e}")
    return NavigateOut(ok=True, current_url=last)


@app.post("/v1/navigate", response_model=NavigateOut, dependencies=[Depends(auth)])
def navigate(body: NavigateIn) -> NavigateOut:
    return _go(str(body.url), body.screen)


@app.post("/v1/home", response_model=NavigateOut, dependencies=[Depends(auth)])
def home(body: ScreenIn | None = None) -> NavigateOut:
    cfg = app.state.cfg
    out = None
    for s in targets(cfg, body.screen if body else None):
        out = _go(s["home_url"], s["name"])   # each screen has its own home
    return out


@app.post("/v1/reload", response_model=NavigateOut, dependencies=[Depends(auth)])
def reload(body: ScreenIn | None = None) -> NavigateOut:
    # ponytail: re-navigate rather than a real reload — same result for a display,
    # and one code path. Use BiDi browsingContext.reload / CDP Page.reload if a
    # page ever needs its POST state kept.
    cfg = app.state.cfg
    out = None
    try:
        for s in targets(cfg, body.screen if body else None):
            out = _go(browser.current_url(cfg, s["name"]), s["name"])
    except (OSError, RuntimeError) as e:
        raise HTTPException(503, f"browser unreachable: {e}")
    return out


@app.post("/v1/scroll", response_model=NavigateOut, dependencies=[Depends(auth)])
def scroll(body: ScrollIn) -> NavigateOut:
    cfg = app.state.cfg
    out = None
    for s in targets(cfg, body.screen):
        display.touch(s)     # reading a long PDF is activity, even without a navigate
        try:
            browser.scroll(cfg, s["name"], dy=body.dy, to=body.to)
        except NotImplementedError as e:
            raise HTTPException(501, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))
        except (OSError, RuntimeError) as e:
            raise HTTPException(503, f"browser unreachable: {e}")
        out = NavigateOut(ok=True, current_url=browser.current_url(cfg, s["name"]))
    return out


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


def _screen_out(s: dict) -> ScreenOut:
    try:
        url = browser.current_url(app.state.cfg, s["name"])
    except (OSError, RuntimeError):
        url = None
    return ScreenOut(name=s["name"], position=s["position"], current_url=url,
                     autoscroll=s["name"] in _autoscroll)


@app.post("/v1/upload", response_model=UploadOut, dependencies=[Depends(auth)])
def upload(request: Request, file: UploadFile,
           screen: str | None = Form(None)) -> UploadOut:
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
    return FileResponse(p, media_type=storage.media_type(file_id),
                        content_disposition_type="inline")


@app.get("/v1/status", response_model=Status, dependencies=[Depends(auth)])
def status() -> Status:
    try:
        url, state = browser.current_url(app.state.cfg), "ok"
    except (OSError, RuntimeError):
        url, state = None, "down"
    # current_url stays the first screen's, so a pre-multi-monitor client that
    # reads it keeps working unchanged.
    return Status(up=True, current_url=url, browser=state,
                  version=os.getenv("ROOM_VERSION", "dev"), awake=display.awake(),
                  screens=[_screen_out(s) for s in app.state.cfg["screens"]])


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
        except (OSError, RuntimeError):
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
