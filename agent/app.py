"""Room display agent — frozen /v1 contract (PLAN.md §5)."""

import contextlib
import os
import secrets
import tomllib
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AnyHttpUrl, BaseModel

from . import browser

DEFAULTS = {
    "kind": "firefox", "path": "", "profile_dir": "", "autolaunch": True,
    "debug_port": 9222,
}


def load_config(path: str | os.PathLike | None = None) -> dict:
    path = Path(path or os.getenv("ROOM_CONFIG") or Path(__file__).parent / "config.toml")
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    cfg["browser"] = DEFAULTS | cfg.get("browser", {})
    if not cfg["browser"]["profile_dir"]:
        cfg["browser"]["profile_dir"] = str(path.parent / "profile")
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
    if proc:
        proc.terminate()


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


class Status(BaseModel):
    up: bool
    current_url: str | None
    browser: str
    version: str


@app.post("/v1/navigate", response_model=NavigateOut, dependencies=[Depends(auth)])
def navigate(body: NavigateIn) -> NavigateOut:
    try:
        url = browser.navigate(app.state.cfg, str(body.url))
    except (OSError, RuntimeError) as e:
        raise HTTPException(503, f"browser unreachable: {e}")
    return NavigateOut(ok=True, current_url=url)


@app.get("/v1/status", response_model=Status, dependencies=[Depends(auth)])
def status() -> Status:
    try:
        url, state = browser.current_url(app.state.cfg), "ok"
    except (OSError, RuntimeError):
        url, state = None, "down"
    return Status(up=True, current_url=url, browser=state,
                  version=os.getenv("ROOM_VERSION", "dev"))
