# Next steps

Findings from an adversarial review of the whole repo, 2026-09-07, and what was
done about them. Suite was green at review time (265 passed, 16 skipped) and is
green now (281 passed, 16 skipped — the skips are still the `ROOM_SMOKE=1`
browser tests, which CI now runs in their own job).

One thing below is **not verified**: the new CI smoke job, which is a workflow
file and is verified by running the workflow. Everything else is covered by the
suite. The `/files` CSP rests on an unmeasured claim about Chromium's PDF
viewer, so the boundary it was meant to provide is carried by a test instead —
see that entry.

## Do these three first

- [x] **Pin the dependencies.** `agent/requirements.in` is now the six names you
  edit; `agent/requirements.txt` is a generated `uv pip compile --universal`
  lock, and every consumer (CI, `setup.sh`, `update.sh`) installs it unchanged.
  `--universal` because one file has to install on the Pi (aarch64), CI (x86_64,
  3.11 and 3.13) and a Windows dev box — it emits markers, so `uvloop` is pinned
  for Linux and `colorama` for Windows in the same file. Suite is green against
  the pinned set.

- [x] **`_home_when_ready` probes the wrong host.** Both halves fixed. The probe
  is now a `home_url` whose path is our own `/home` — on the Pi the full tailnet
  url, which is the address that is actually listening, since the unit passes
  `--host $(tailscale ip -4)` and never reads `[server]`. And the wait is
  best-effort: every screen is navigated whether or not the probe ever answered.
  `tests/test_resilience.py` pins both.

  *Deviation from the finding:* it said probe `cfg["server"]["host"]`. That is
  `127.0.0.1` on the Pi, where nothing listens — see the unit's `ExecStart`.

- [x] **`PUT /v1/settings` truncates `settings.json`.** `settings.merge_screens`
  overlays the edit onto the saved list by index and keeps any tail the editor
  could not see, so a save with a monitor unplugged no longer drops the other
  screen. Covered at the unit level and through the route.

## Real bugs, lower blast radius

- [x] **BiDi socket shared across threadpool threads, no lock.** One
  `threading.Lock` around `_bidi()` and `close()`; `_bidi_connect` runs under
  it, so `session.new` cannot race either. `test_two_threads_never_share_the_bidi_socket`
  drives four threads at it and asserts nothing overlapped and nobody got
  somebody else's reply.

- [x] **`sweep()` crashes on concurrent uploads.** Files that vanish between the
  glob and the stat are skipped. The test races a deletion into the middle of
  the glob and asserts the upload after it still works.

- [x] **`extensions.install` caps the download but not the extraction.** Sums
  `z.infolist()` file sizes before `extractall`. The test builds a genuinely
  compressed 8 MB bomb that slips under the 1 MB download cap.
  Marked `ponytail:` — `file_size` is the archive's own claim, so this bounds
  the honest-but-huge case; metering the extract stream is the upgrade if a
  hostile CRX is ever in scope.

## Contract inconsistencies

- [x] **`/v1/autoscroll` returns a screen name in `current_url`.** Documented as
  frozen, in the route docstring and in README under "Two frozen warts", next to
  the same note on `up`.

- [x] **Three definitions of `home_url`.** One now: `app._home_url()`, called
  from `load_config`. A path resolves against `[server]`, so `"/home"` really is
  this agent's idle page and the shipped example is loadable as written;
  anything that is not http, https or `about:` is refused at load. There is a
  test that loads `config.example.toml` verbatim, which is what would have
  caught this.

## CI and deploy

- [x] **CI never exercises the protocol code.** New `smoke` job on
  `ubuntu-latest`: symlinks the runner's preinstalled Chrome to `chromium`,
  relaxes the 24.04 AppArmor userns restriction (Chromium's sandbox needs it —
  better than `--no-sandbox`, which no Pi runs), and runs `tests/test_smoke.py`
  under `xvfb-run` with `ROOM_SMOKE=1`. `chromium --version` runs first and
  fails the job loudly, because the kiosk fixture *skips* when it finds no
  binary and a silent skip is what let this gap last.

  ⚠️ **Unverified.** GitHub Actions cannot be run from here. If it goes red on
  the first push, the likely causes in order are: the AppArmor sysctl, `--kiosk`
  under a bare Xvfb with no window manager, and Chrome's sandbox on the runner.

- [x] **`VERIFY_TAG` defaults to 0 and the signing path is untested.** Default
  unchanged — that argument still holds. `test_verify_tag_actually_refuses_an_unsigned_tag`
  now builds a real git repo with a real unsigned tag and runs the real
  `update.sh`: it asserts exit 1, the message, the `.failed-<tag>` latch, and
  that `current` never appeared. Checked by hand that the gate is what stops it
  — with `VERIFY_TAG=0` the same script runs on to the venv build.

  The *accept* half still needs a key, so it is a documented drill rather than a
  test: `deploy/pi/smoke-on-the-pi.md` §"What this does not cover" now carries
  the command and what a good run prints.

- [x] **Naive TOML parse for the token.** `| head -1`, with a test that runs the
  real pipeline against a config holding two `token =` lines.

## Hardening (cheap)

- [x] **`Content-Security-Policy: sandbox` on `/files`.** Shipped as
  `sandbox allow-scripts`. The defence is the **opaque origin**, which is what
  puts the web UI's `localStorage` out of reach; `allow-scripts` is there
  because a bare `sandbox` is *believed* to render Chromium's built-in PDF
  viewer blank, and PDFs are half of what this display is for.

  Believed, not measured — so the header is not what the boundary rests on.
  `test_nothing_in_types_can_execute` is: no entry in `storage.TYPES` is a
  script-bearing document, and adding one fails the suite with a pointer back
  here. That check needs no browser and holds whether or not my reading of
  `allow-scripts` is right.

  Still worth doing on the Pi once: load a PDF and a video through `/files` with
  the header on. If PDFs render fine under a bare `sandbox`, drop
  `allow-scripts` — it buys nothing today.

## Over-engineering

- [x] **Prose outweighs code.** Three passes over the six agent modules, stating
  each fact once. Comments+docstrings vs. code, before → after:

  | file | before | after |
  |---|---|---|
  | `app.py` | 416/584 (0.71) | 373/590 (0.63) |
  | `browser.py` | 466/612 (0.76) | 417/611 (0.68) |
  | `storage.py` | 47/49 (0.96) | 43/53 (0.81) |
  | `display.py` | 81/88 (0.92) | 64/86 (0.74) |
  | `settings.py` | 50/38 (1.32) | 41/39 (1.05) |
  | `extensions.py` | 50/77 (0.65) | 46/77 (0.60) |
  | **total** | **1110/1448 (0.77)** | **984/1456 (0.68)** |

  126 lines of prose gone, and the drift the finding actually named
  (`_home_when_ready`) is fixed. **Stopped deliberately at 0.68.** Nothing was
  moved to PLAN.md: relocating the reasoning keeps the maintenance and adds a
  cross-file sync problem, which is the complaint this file makes elsewhere
  about `SUPPORTS`. Getting below ~0.5 means deleting *facts* — recorded Pi-only
  bugs, hardware quirks, protocol constraints — and which of those to lose is a
  call worth making deliberately rather than in a cleanup pass.

- [x] **Six copies of the Firefox guard.** One `_require(cfg, "scroll")` reading
  `SUPPORTS`, replacing all six plus `_require_cdp`. The hand-sync note at the
  top of `browser.py` is gone because the table *is* the enforcement now.
  `/v1/autoscroll`'s own route-level guard reads the same table (it has to stay:
  the autoscroll thread suppresses everything `browser.autoscroll` raises).

- [x] **`roomctl/__init__.py:248-333` is 86 lines of pure delegation.** Deleted.
  The CLI now opens one `Client` per command instead of dialling inside every
  lambda.

  ⚠️ **Breaking, and worth a version bump.** `roomctl.status(target)`,
  `roomctl.navigate(url, target, screen)` and the other twelve are gone;
  `roomctl.Client` and `roomctl.client` are unchanged, and the `roomctl` CLI is
  unaffected. README carries the migration line.

- [x] **`Status.up` is always `true`.** Already documented in README; now under
  the "Two frozen warts" heading with `/v1/autoscroll`, and the model comment
  points at it instead of restating the argument.
