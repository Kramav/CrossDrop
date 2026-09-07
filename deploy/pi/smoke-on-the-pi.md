# Running the smoke suite on the Pi

`pytest` on its own needs no browser and proves the agent's logic. The **smoke**
suite is the other half: it drives a real Chromium and checks the things a stub
cannot, which is exactly the set that differs between your laptop and the box on
the wall — real dual monitors, a real compositor, and Debian's Chromium build
rather than Google's.

Run it after a deploy that touched `agent/browser.py`, and after any Pi OS
upgrade that moved Chromium.

**It takes over the display for about half a minute.** Do it when nobody is
looking at the wall.

---

## What it actually proves

Three things, none of which survives being mocked:

- **A capture is in CSS pixels.** It asks for a 200×100 region and checks the
  PNG *header* says 200×100. On a HiDPI panel an unpinned capture comes back
  doubled, and every coordinate a client derives from the picture is then off by
  a factor of two — including the web UI's click-the-picture-to-click-the-page.
- **`error_page` is real.** It navigates to a dead port and confirms Chromium's
  own error page is detected, while `/v1/status` still cheerfully says
  `browser: "ok"`. That flag is what `update.sh` rolls a release back on.
- **A click lands where it was aimed.** Proved by the page changing, not by
  anyone looking at it — a button at a known position, clicked by coordinate,
  and a miss checked in the other direction so a click that fires on everything
  cannot pass.

Plus typing into a focused field, Enter submitting a form, a failed step
stopping the ones after it, and the whole look → click → type → verify workflow.

---

## 1. Get in, and get a display

```sh
ssh room@<pi-tailnet-ip>
cd /opt/room-display/current
```

Two things an SSH session does not give you, and both are needed here:

```sh
export XDG_RUNTIME_DIR=/run/user/$(id -u)          # for systemctl --user
export DISPLAY=:0                                  # the tests launch a browser
```

`DISPLAY` is the one people forget. Without it Chromium exits with *"Missing X
server or $DISPLAY"* and the fixture reports `browser never came up` — which
reads like a code problem and is not one.

---

## 2. Stop the agent first

**This is not optional.** The agent's kiosk holds debug port 9222, and the smoke
suite needs it. The tests refuse to run otherwise:

```
debug port 9222 is already in use — almost certainly the live agent.
```

That guard exists because the failure it replaces was silent *and* destructive:
the test browser could not bind the port, the fixture connected to the running
kiosk instead, and every test drove the real display before teardown shut it
down.

```sh
systemctl --user stop display-agent
```

The wall goes dark for the duration. Nothing else is affected — your token,
logins, screen settings and uploads all live outside the release tree.

---

## 3. Run it

```sh
ROOM_BROWSER=chromium ROOM_SMOKE=1 .venv/bin/python -m pytest tests/test_smoke.py -q
```

- `ROOM_SMOKE=1` is what un-skips the file. Without it you get `13 skipped`.
- `ROOM_BROWSER=chromium` is the default on the Pi, but being explicit costs
  nothing and makes the command copy-pasteable onto a dev box.
- `.venv/bin/python -m pytest`, not bare `pytest`: `-m` is what puts the release
  directory on `sys.path`, and the release venv already has pytest — it is in
  `agent/requirements.txt`.

Expect **13 passed in ~20s**, and one fullscreen browser window that opens,
does its work and closes itself.

Want to watch it happen, or narrow to one thing:

```sh
... -m pytest tests/test_smoke.py -q -k click     # just the input tests
... -m pytest tests/test_smoke.py -v              # name each test as it runs
... -m pytest tests/test_smoke.py -x --tb=long    # stop at the first failure
```

---

## 4. Put it back

```sh
systemctl --user start display-agent
systemctl --user is-active display-agent
```

Whatever was on the wall comes back on its own: the agent records what each
screen was showing at shutdown and restores it at start, within
`display.restore_within_minutes` (12 hours by default). That is the same
mechanism that makes the nightly 04:00 restart invisible.

Confirm from a controller box rather than from the Pi — the point is that it
answers over the tailnet:

```sh
roomctl status | jq '{version, browser, error}'
roomctl shot -o wall.png
```

**Do not leave the agent stopped.** While it is down nothing is managing display
power, and on a box with no keyboard a monitor that blanks on the session's own
timeout has nothing to wake it.

---

## Troubleshooting

**`debug port 9222 is already in use`** — §2. Something is on that port: the
agent, or a browser a previous run leaked. Check and clear it:

```sh
systemctl --user stop display-agent
curl -s http://127.0.0.1:9222/json/version         # anything still answering?
pkill -f 'remote-debugging-port=9222'              # last resort
```

**`browser never came up: {...}`** — the fixture waited 30s and gave up. The
status dict it prints carries `error`, which is the reason. Usually `DISPLAY` is
unset (§1). If it names a missing binary, check `browser.path` in
`/etc/room-display/config.toml`.

**13 skipped** — `ROOM_SMOKE=1` was not set, or `ROOM_BROWSER` says `firefox`.
Everything here is CDP-only.

**Input tests fail with 501** — the smoke fixture writes its own config with
`[interact] enabled = true`, so this should not happen. If it does, the release
predates `/v1/input`; check `git -C /opt/room-display/cache-repo describe`
against what `current` points at.

**A test fails on a real browser but passes on a desktop** — that is the suite
doing its job, and worth reporting rather than re-running. The likely culprits
are the ones that differ: monitor geometry, device pixel ratio, and Debian's
Chromium version against Google's.

**Leftover browser after a failure** — teardown asserts the browser is gone, so
a failure there means one is still holding 9222 and the agent will not come back
cleanly. Clear it with the `pkill` above before starting the agent.

---

## What this does not cover

- **Whether the picture looks right.** The suite checks dimensions, formats and
  that clicks land. Nobody but you can say the capture resembles the wall — that
  is `roomctl shot -o wall.png` and your own eyes.
- **The rollback.** `update.sh`'s health gate and its failure screenshot are a
  separate drill: tag a deliberately broken commit on the spare Pi and watch it
  come back. See [update-over-ssh.md](update-over-ssh.md) §5.
- **Multi-monitor placement.** The smoke config declares a single screen, so
  window placement across two panels is still verified by looking.
