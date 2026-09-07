# Next steps

Findings from an adversarial review of the whole repo, 2026-09-07. Suite was green
at the time (265 passed, 16 skipped — the skips are all `ROOM_SMOKE=1` browser
tests). Ordered by what costs most if left alone.

Browser-protocol findings are by inspection only; no real browser was driven.

## Do these three first

- [ ] **Pin the dependencies.** `agent/requirements.txt` has six unpinned names, and
  `update.sh` builds a fresh venv per release from live PyPI — so the artifact CI
  proved green is not the artifact the Pi builds. `selfcheck` catches import
  breakage, not behaviour changes. `pip-compile` it; rollback still works because
  each release dir keeps its own venv.

- [ ] **`_home_when_ready` probes the wrong host.** `agent/app.py:266-277` picks the
  first http `home_url` across all screens, but the docstring says it waits for
  "the port". If screen 1's home is an external site that is down, the loop burns
  60s, hits `else: return`, and *no* screen is navigated — leaving screen 2, whose
  home is the agent's own `/home`, on the "can't be reached" page this function
  exists to prevent. No retry after. Probe `cfg["server"]["host"]` and navigate
  per-screen regardless.

- [ ] **`PUT /v1/settings` truncates `settings.json`.** `agent/app.py:1023` +
  `agent/settings.py:99`. `settings.save(data)` rewrites the file whole. Unplug a
  monitor → restart → `display.detect()` returns 1 screen → the UI can only edit 1
  → the save drops the second screen's saved name and home_url for good. Merge into
  the loaded list instead of replacing it.

## Real bugs, lower blast radius

- [ ] **BiDi socket shared across threadpool threads, no lock.**
  `agent/browser.py:1198-1226`. Every browser route is `def`, so two concurrent
  Firefox requests send and `recv()` on the same websocket; the id-matching loop
  means one thread eats the other's reply and the loser blocks to its 15s timeout.
  `_bidi_connect` can also race two `session.new` calls and leak the losing socket.
  Dev-box only. A `threading.Lock` around `_bidi` is three lines.

- [ ] **`sweep()` crashes on concurrent uploads.** `agent/storage.py:113` —
  `sorted(glob("*"), key=lambda p: p.stat().st_mtime)` raises `FileNotFoundError`
  if another upload's sweep unlinks a file between the glob and the stat.
  `save()` catches `BaseException` and re-raises → 500 on a valid upload. Skip
  files that vanish.

- [ ] **`extensions.install` caps the download but not the extraction.**
  `agent/extensions.py:101-116`. `read(cap + 1)` bounds the CRX at 50 MB;
  `z.extractall()` is unbounded, so a zip bomb writes gigabytes to the SD card —
  the storage this whole design protects. The path-traversal reasoning in the
  comment is right; the size reasoning stops one step early. Sum
  `z.infolist()` file sizes first.

## Contract inconsistencies

- [ ] **`/v1/autoscroll` returns a screen name in `current_url`.** `agent/app.py:765`
  — and only the last screen's, with an empty `screens` list, unlike every other
  fan-out route. `/v1` is frozen so it stays; document it as a wart.

- [ ] **Three definitions of `home_url`.** `config.example.toml:5` ships `/home`;
  `PUT /v1/settings` 422s exactly that (`app.py:1032`); `_home_when_ready` skips it
  as a probe; `/v1/home` passes it straight to `Page.navigate`, bypassing the
  `AnyHttpUrl` check `/v1/navigate` enforces. Validate once in `load_config` and
  make the example match.

## CI and deploy

- [ ] **CI never exercises the protocol code.** `browser.py` is the riskiest 1,230
  lines in the repo and every test that speaks real CDP is skipped in CI. Two
  comments in that file record bugs found *only* on the Pi (`browser.py:938-945`,
  "passed against desktop Chrome every time"). `ubuntu-latest` has Chromium —
  `xvfb-run` + `ROOM_SMOKE=1` as one CI step closes most of the gap.

- [ ] **`VERIFY_TAG` defaults to 0** (`deploy/pi/update.sh:64`). The comment states
  the consequence: push access to the repo is code execution on every Pi within 30
  minutes. The default is defensible; the signing path being untested is not —
  nothing in CI or the smoke docs runs `VERIFY_TAG=1`.

- [ ] **Naive TOML parse for the token.** `deploy/pi/update.sh:110` — a second
  matching line makes `TOKEN` multi-line, which fails the health check and triggers
  a *false rollback*. `head -1` at minimum.

## Hardening (cheap)

- [ ] **`Content-Security-Policy: sandbox` on `/files`.** `agent/app.py:1118`.
  `/files/{id}` is unauthenticated and same-origin with the web UI, which holds the
  bearer token in `localStorage`. Safe today — no SVG or HTML in `TYPES`, `nosniff`
  set — but the whole defense rests on nobody ever adding a type. One header makes
  the boundary structural instead of a rule to remember, the same argument
  `ci.yml:38-41` makes for the CDP transport boundary.

## Over-engineering

- [ ] **Prose outweighs code.** Comments + docstrings vs. code lines:
  `app.py` 408/567, `browser.py` 416/638, `storage.py` 40/53,
  `display.py` **91/71**. The comments are unusually good — all *why*, not *what* —
  but at this density they are a second artifact to maintain, and they have already
  drifted (see `_home_when_ready` above). Rule of thumb: if the reasoning is longer
  than the function, it belongs in PLAN.md with a one-line pointer.

- [ ] **Six copies of the Firefox guard.** `browser.py` lines 426, 475, 718, 791,
  846, 925. `SUPPORTS` already declares which backend does what, and the comment at
  `browser.py:57` admits the two are kept in step by hand. One `_require(cfg,
  "scroll")` reading the table replaces all six and deletes the hand-sync.

- [ ] **`roomctl/__init__.py:248-333` is 86 lines of pure delegation.** Fourteen
  module functions that each do `with client(target) as c: return c.method(...)`.
  Every new endpoint is written three times (Client method, module function, CLI
  entry). Keep `Client`; the by-name layer earns little.

- [ ] **`Status.up` is always `true`** and documented as useless (`app.py:581-588`).
  Frozen contract, so it stays — note in the API docs that the field is decorative.
