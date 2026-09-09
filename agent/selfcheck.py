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
    # A v1 box still has the frozen unit setup.sh wrote, which sets ROOM_CONFIG
    # and points WorkingDirectory at /opt/room-display/current. update.sh never
    # rewrites unit files, so that unit survives every tag -- including the one
    # that renamed everything.
    #
    # Refuse rather than tolerate. update.sh gates the symlink swap on this, so
    # an unmigrated Pi reaching the rename tag keeps running the release it has,
    # stays healthy, and prints the fix into its own journal. A compatibility
    # fallback would instead make the half-migrated state *work*, which means
    # nobody ever migrates and the fallback becomes permanent.
    if os.getenv("ROOM_CONFIG") and not os.getenv("CROSSDROP_CONFIG"):
        print("selfcheck: FAIL this box is still on the v1 layout (ROOM_CONFIG "
              "is set, CROSSDROP_CONFIG is not). Nothing was changed. Migrate "
              "it first:\n"
              "  bash /opt/room-display/current/deploy/pi/migrate.sh",
              file=sys.stderr)
        return 1
    os.environ["CROSSDROP_SELFCHECK"] = "1"      # before the app imports: no kiosk
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
