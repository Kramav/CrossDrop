"""Boot sanity for a freshly built release: `python -m agent selfcheck`.

Exit 0 means this build loads its config, imports cleanly, starts the app and
answers `/v1/status`. Exit 1 means do not swap to it -- update.sh gates the
symlink on this, so a release that cannot import never reaches the screen.

Runs entirely in-process: no port is bound and no browser is launched, so it is
safe to run while the live agent is up. That is also its limit -- it catches
syntax, import, dependency and config breakage, not runtime or browser
regressions. Those are caught after the swap by polling the real /v1/status,
which is what actually triggers a rollback.
"""

import os
import sys


def selfcheck() -> int:
    os.environ["ROOM_SELFCHECK"] = "1"      # before the app imports: no kiosk
    try:
        from fastapi.testclient import TestClient

        from .app import app, load_config

        cfg = load_config()
        with TestClient(app) as client:     # runs lifespan, minus the browser
            r = client.get("/v1/status",
                           headers={"Authorization": f"Bearer {cfg['token']}"})
    except Exception as e:                  # any import/config failure lands here
        print(f"selfcheck: FAIL {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if r.status_code != 200:
        print(f"selfcheck: FAIL /v1/status -> {r.status_code} {r.text}", file=sys.stderr)
        return 1
    print(f"selfcheck: ok {r.json()}")
    return 0
