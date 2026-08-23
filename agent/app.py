"""Room display agent — frozen /v1 contract (PLAN.md §5)."""

import contextlib
import os
import secrets
import tomllib
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AnyHttpUrl, BaseModel

from . import browser, storage

DEFAULTS = {
    "kind": "firefox", "path": "", "profile_dir": "", "autolaunch": True,
    "debug_port": 9222,
}
UPLOAD_DEFAULTS = {"dir": "", "max_mb": 25, "keep": 20}


def load_config(path: str | os.PathLike | None = None) -> dict:
    path = Path(path or os.getenv("ROOM_CONFIG") or Path(__file__).parent / "config.toml")
    # utf-8-sig: Windows editors and PowerShell write a BOM that tomllib chokes on.
    cfg = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    cfg["browser"] = DEFAULTS | cfg.get("browser", {})
    cfg["upload"] = UPLOAD_DEFAULTS | cfg.get("upload", {})
    if not cfg["browser"]["profile_dir"]:
        cfg["browser"]["profile_dir"] = str(path.parent / "profile")
    if not cfg["upload"]["dir"]:
        cfg["upload"]["dir"] = str(path.parent / "uploads")
    if not cfg.get("token"):
        raise RuntimeError(f"{path}: token is required")
    return cfg


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.cfg = cfg = load_config()
    # We own the kiosk only if we started it — a no-input box must not be left
    # with an orphaned fullscreen window after the agent stops.
    proc = browser.launch(cfg) if cfg["browser"]["autolaunch"] else None
    yield
    browser.close()
    if proc:
        browser.stop(proc)


app = FastAPI(title="room-display agent", version="1", lifespan=lifespan)
_bearer = HTTPBearer(auto_error=True)


def auth(app_cfg: HTTPAuthorizationCredentials = Depends(_bearer)) -> None:
    if not secrets.compare_digest(app_cfg.credentials, app.state.cfg["token"]):
        raise HTTPException(401, "bad token")


class NavigateIn(BaseModel):
    url: AnyHttpUrl  # http/https only — scheme allowlist per PLAN.md §10


class NavigateOut(BaseModel):
    ok: bool
    current_url: str


class UploadOut(BaseModel):
    id: str
    url: str


class Status(BaseModel):
    up: bool
    current_url: str | None
    browser: str
    version: str


# Every route that touches the browser is `def`, not `async def`: browser.py is
# blocking websocket I/O, and on the event loop it starves uvicorn hard enough
# that the BiDi handshake fails. Sync routes get FastAPI's threadpool.
def _go(url: str) -> NavigateOut:
    try:
        return NavigateOut(ok=True, current_url=browser.navigate(app.state.cfg, url))
    except (OSError, RuntimeError) as e:
        raise HTTPException(503, f"browser unreachable: {e}")


@app.post("/v1/navigate", response_model=NavigateOut, dependencies=[Depends(auth)])
def navigate(body: NavigateIn) -> NavigateOut:
    return _go(str(body.url))


@app.post("/v1/home", response_model=NavigateOut, dependencies=[Depends(auth)])
def home() -> NavigateOut:
    return _go(app.state.cfg["home_url"])


@app.post("/v1/reload", response_model=NavigateOut, dependencies=[Depends(auth)])
def reload() -> NavigateOut:
    # ponytail: re-navigate rather than a real reload — same result for a display,
    # and one code path. Use BiDi browsingContext.reload / CDP Page.reload if a
    # page ever needs its POST state kept.
    try:
        return _go(browser.current_url(app.state.cfg))
    except (OSError, RuntimeError) as e:
        raise HTTPException(503, f"browser unreachable: {e}")


@app.post("/v1/upload", response_model=UploadOut, dependencies=[Depends(auth)])
def upload(request: Request, file: UploadFile) -> UploadOut:
    try:
        file_id = storage.save(app.state.cfg, file.filename,
                               iter(lambda: file.file.read(1 << 20), b""))
    except storage.BadType as e:
        raise HTTPException(415, str(e))
    except storage.TooBig as e:
        raise HTTPException(413, str(e))

    # The kiosk browser is on this same box, so send it to loopback: it works
    # whether the uploader reached us by tailnet name, IP, or localhost.
    served = request.url_for("serve_file", file_id=file_id).replace(hostname="127.0.0.1")
    _go(str(served))
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
    return Status(up=True, current_url=url, browser=state,
                  version=os.getenv("ROOM_VERSION", "dev"))


# Unauthenticated on purpose: you need the page before you can type the token.
# It ships no secrets — every /v1 call it makes carries the bearer header.
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent.parent / "web" / "index.html")
