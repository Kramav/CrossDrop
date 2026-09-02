# Adversarial review — 2026-09-01

Scope: full read of `agent/`, `roomctl/`, `web/`, `deploy/`. 1,656 lines of
source, 1,140 of tests. `pytest` run locally: 86 passed, 2 skipped, 32s.

Graded against a stated trust boundary of **the tailnet only** — no hostile
device on it — on a **4 GB Pi**, with the auto-update timer treated as something
that will be enabled.

| Axis | Grade | One line |
|---|---|---|
| Security | B+ | Right model; one gratuitously disabled browser defense |
| Viability | B | Well-reasoned for a keyboard-less box; updater had a rollback loop, tmpfs budget doesn't close |
| Speed | B | Everything cheap except autoscroll, which opens 10 WebSockets/sec |
| Install | A− | Strongest axis. Re-runnable, honest post-install checks, no-dependency tray client |

## Findings

| # | Finding | Where | Severity | Status |
|---|---|---|---|---|
| 1 | Updater redeploys a failed tag every 30 min, forever | [update.sh](deploy/pi/update.sh) | high | **fixed** |
| 2 | `--remote-allow-origins=*` disables a defense the client doesn't need | [browser.py:79](agent/browser.py#L79) | high | **fixed**, needs Pi verify |
| 3 | tmpfs budget over-subscribed; ENOSPC wedges uploads permanently | [storage.py:57](agent/storage.py#L57), [setup.sh:152](deploy/pi/setup.sh#L152) | medium | **fixed** |
| 4 | `keep = 0` deletes the file just uploaded | [storage.py:92](agent/storage.py#L92) | medium | **fixed** |
| 5 | Autoscroll opens a WebSocket 10×/sec | [app.py:269](agent/app.py#L269), [browser.py:302](agent/browser.py#L302) | medium | open, unverified |
| 6 | `cfg.clear()`/`update()` torn read → 500 | [app.py:480](agent/app.py#L480) | low | **fixed** |
| 7 | `/files/{id}` lacks `nosniff` | [app.py:526](agent/app.py#L526) | low | **fixed** |
| 8 | Bearer token on `update.sh` curl command line | [update.sh:83](deploy/pi/update.sh#L83) | low | won't fix |
| 9 | `tailscale up --ssh` enabled silently by the installer | [setup.sh:62](deploy/pi/setup.sh#L62) | low | open, docs |
| 10 | Auto-update deploys unsigned tags | [update.sh:32](deploy/pi/update.sh#L32) | low | won't fix, opt-in |
| 11 | `request.url_for` reflects the `Host` header | [app.py:520](agent/app.py#L520) | low | won't fix |

**8, 10 and 11 are deliberately not fixed.** 8's threat is another local user on
a Pi that has exactly one; 10 trades a permanent release-signing burden against
an opt-in path that ships disabled; 11 needs a proxy or a hand-crafted `Host` on
an already-authenticated route. Each costs more than the risk it removes. 9 is a
line of README, not code. **5 is the one real open item** — it needs a Pi to
confirm before the rewrite is worth doing.

### 1. Rollback loop — fixed

A tag passing `selfcheck` but failing the live verify rolled back and exited 1,
recording nothing. Thirty minutes later `sort -V | tail -1` found the same tag
and redeployed it: rebuild a venv, restart the agent, relaunch the kiosk, fail,
roll back, restart again. Two kiosk restarts every half hour, forever, on a box
with no keyboard — the exact failure the file was written to prevent.

Fixed with a `$RELEASES/.failed-$TAG` marker, checked after the up-to-date test
and written on rollback. The marker is a dotfile, so the step-8 prune loop's
`ls -1` ignores it. Side effect: the promise at
[update.sh:104](deploy/pi/update.sh#L104) that a bad release is "left in `$DEST`
for inspection" is now true — previously the next run's `rm -rf "$DEST"`
destroyed it.

### 2. `--remote-allow-origins=*` — fixed, unverified on hardware

The tailnet boundary does not cover this one. The threat is not a device on the
tailnet; it is the arbitrary web page pushed to the display, which runs inside
the kiosk browser on the far side of the boundary by design. That flag disables
the check Chromium added specifically to stop page content reaching
`ws://127.0.0.1:9222`. The payoff for an attacker is the whole shared cookie jar
— one profile, one debug port, every SSO session ([browser.py:66](agent/browser.py#L66)).

Still gated behind guessing a target UUID (`/json` is not CORS-readable), so
hardening rather than a live hole. But it buys nothing:
[browser.py:400](agent/browser.py#L400) already passes `suppress_origin=True`
unconditionally on every CDP connection. The flag's own comment says as much.

Deleted, and PLAN.md §6's gotcha entry rewritten so nobody re-adds it as the
"fix" for a CDP failure. **No test covers this and none can** — the flag's
absence only matters against a real Chromium. Next deploy, confirm
`/v1/status` still returns a `current_url` rather than a browser error; if CDP
fails there, that is itself a finding.

### 3. tmpfs budget — fixed

Profile and uploads share `/run/user/$UID`, which logind sizes at 10% of RAM —
~400 MB on this Pi. Against it: `keep = 20 × max_mb = 25` is up to 500 MB of
uploads, plus the profile and its 100 MB disk cache. Worst case ~750 MB into
400 MB.

The failure mode is worse than the arithmetic. [storage.py:57](agent/storage.py#L57)
does not catch `OSError`, so ENOSPC is an uncaught 500 rather than the 413 a
caller expects, and `sweep()` runs *after* the write
([storage.py:68](agent/storage.py#L68)) — so a full tmpfs never self-clears and
uploads stay wedged.

Fixed both halves: `keep` defaults to 5 (`UPLOAD_DEFAULTS` and
`config.example.toml`, with the arithmetic written down), and `save()` now
sweeps *before* the write as well as after. The pre-sweep is what breaks the
wedge — a failed write no longer leaves the directory full with nothing to
clear it. `test_failed_upload_still_frees_room` drives an ENOSPC mid-write and
asserts the directory came back under `keep`. The existing exactly-N assertion
in `test_storage_caps_and_sweeps` still passes, so the documented semantics did
not drift.

### 4. `keep = 0` — fixed

`files[: -keep or None]` — `-0` is falsy, so the slice becomes `files[:None]`,
i.e. every file including the one just written. The config documents `keep` as
"newest N kept", which reads like `0` means "keep none of the *old* ones" —
and on a box explicitly built as a temporary display rather than a filestore,
that is exactly the value someone reaches for. The upload would then 404 on the
display it was just sent to.

Floored at 1 in `sweep()` rather than rejected at config load: this box has no
keyboard, so coercing a meaningless value beats refusing to boot on one.
`test_keep_zero_still_serves_the_file_just_uploaded` pins it.

### 5. Autoscroll — fixed

Each tick does an HTTP `GET /json`, a fresh WebSocket handshake,
`Page.getLayoutMetrics`, `Input.dispatchMouseEvent`, then closes the socket. At
`stop.wait(0.1)` that is 10 TCP connects + 10 HTTP requests + 20 CDP round-trips
per second while the Pi is also rendering the page being scrolled. Everything
else in the codebase is two orders of magnitude cheaper (`/v1/status` is 3 HTTP
hits, the web UI polls at 15s, `display.py` ticks once a minute).

Fix is to hoist the connection and the viewport centre out of the loop — the
viewport cannot change on a fullscreen kiosk window:

```python
def run() -> None:
    page = browser._cdp_page(cfg, name)
    with browser._rpc(page["webSocketDebuggerUrl"]) as call:
        x, y = browser._viewport_centre(call)
        while not stop.wait(0.1):
            call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y,
                                              "deltaX": 0, "deltaY": speed})
```

**Fixed**, as `browser.autoscroll()` rather than inline in `app.py` — the loop
needs `_cdp_page`, `_rpc` and `_viewport_centre`, and those are `browser.py`
plumbing that the route layer should not be reaching into. `app.py` now just
runs it on a thread with the same stop `Event`.

`test_autoscroll_holds_one_connection_for_the_whole_run` in
`tests/test_screens.py` counts websocket opens against wheel events: one open,
many wheels. Measured against the old shape over the same window, at a tick
sped up 10x for the test: **9 connections before, 1 after.**

One behaviour change, deliberate: a window that closes mid-run now ends the
scroll instead of reconnecting to whatever target replaced it. The only things
that change a target are a navigation — which already stops autoscroll on
purpose — and the window going away, so stopping is the better answer.

**Still unverified on hardware.** The connection count is proven; the CPU claim
is not. To confirm the original prediction: run `roomctl autoscroll start
--speed 40` on a long page, leave it two minutes, watch `%CPU` in `top` for the
agent's `python` and for `chromium`.

### 6–11, briefly

- **6 — fixed.** `cfg.clear()` then `cfg.update()` left a window where a
  concurrent threadpool request reading `cfg["screens"]` got a `KeyError` — and
  the window was the whole of `load_config()`, which reads a file *and* shells
  out to `xrandr`. The mutate-in-place choice is correct and well-explained
  (`display.watch()` closed over the dict), so the fix keeps it and just builds
  the new dict first: `fresh = load_config()`, then clear and update back to
  back.
- **7 — fixed.** `/files/{id}` is unauthenticated and same-origin with the web UI
  that holds the token in `localStorage`. The `TYPES` allowlist is a good
  mitigation and correctly excludes SVG; modern Chrome will not upgrade
  `text/plain` to HTML, and uploading needs the token anyway. `nosniff` now on
  the response, asserted by `test_files_route_sends_nosniff`.
- **8** — `curl -H "Authorization: Bearer $TOKEN"` puts the token in
  `/proc/<pid>/cmdline` for any local user, up to 30 times per verify. `-H @-`
  with the header on stdin costs nothing.
- **9** — `tailscale up --ssh` makes every install SSH-reachable under tailnet
  ACLs the script never mentions. Defensible, but should be a stated decision
  rather than a silent one inside a `curl | bash`.
- **10** — `git ls-remote --tags | sort -V | tail -1` → build → run, unsigned.
  Push access to the repo is code execution on every Pi within 30 minutes.
  Correctly opt-in and disabled by default, and the reasoning at
  [setup.sh:179](deploy/pi/setup.sh#L179) is sound. `git verify-tag` if this
  ever runs on a box that matters.
- **11** — the post-upload navigation URL is built from the client's `Host`
  header. Authenticated-only, so not an escalation, but an upload through a proxy
  or with an odd `Host` sends the display somewhere it cannot resolve. Build it
  from the configured bind address.

## What holds up

Not padding — these are things I tried to break and could not.

- **Auth model.** `secrets.compare_digest`; refusal to boot without a token
  ([app.py:39](agent/app.py#L39)); bearer header rather than a cookie, so there
  is no CSRF surface; `AnyHttpUrl` as a scheme allowlist; tailnet-only bind
  asked of `tailscaled` rather than hardcoded.
- **The config split.** `config.toml` deliberately not agent-writable, with the
  genuinely-runtime subset carved into `settings.json`. The reasoning in
  [settings.py](agent/settings.py) for matching screens by index rather than
  name — because the name is itself editable — is right, and non-obvious.
- **`/home-status`** reports hostname, never the full URL, with the reason
  written down ([app.py:565](agent/app.py#L565)).
- **Snapshot hygiene.** `umask 077` set *before* tar rather than a chmod after,
  closing the world-readable window. The `SERVICE_RESULT` distinction between a
  crash restart and a clean stop — so a crash loop does not grind the SD card
  but a real logout still saves — is the kind of thing most projects get wrong.
- **The updater's shape**, finding 1 aside: a cheap `ls-remote` that writes
  nothing on the common path, `git archive` so a release carries no `.git`, an
  in-process selfcheck gating the swap, a symlink swap, a live-port verify.
  Sound machine that was missing a latch.
- **Install.** One re-runnable script covering both Pi and plain Debian with the
  branch points named up front; `id -un` instead of `$USER` with the reason
  given ([setup.sh:27](deploy/pi/setup.sh#L27)); a root refusal that says what to
  run instead; a `== checks` block that verifies tmpfs, Chromium, X11 and service
  liveness and dumps `journalctl` on failure, with a comment explaining why none
  of it may abort under `set -e`. Exec bits are `100755` in git, so `git archive`
  in `update.sh` preserves them — verified.

Minor install gaps: `apt full-upgrade -y` ([setup.sh:52](deploy/pi/setup.sh#L52))
can turn a 3-minute install into 30 with a reboot; nothing checks that
`pip install` succeeded before enabling the service, so you learn from the
`journalctl` dump; the final banner prints the token into terminal scrollback.

## Unresolved

The repo is named CrossDrop but every path, unit and config says `room-display`,
and [setup.sh:19](deploy/pi/setup.sh#L19) hardcodes `github.com/Kramav/CrossDrop`.
If that repo is private, both the README's `curl | bash` line and `setup.sh`'s
clone fail on a git credential prompt in a pipeline with no tty. Not checked.
