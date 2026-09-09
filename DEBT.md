# Debt ledger

Findings that survived an adversarial review round without being fixed, and why.
Everything Critical or High from every round was fixed; this is what was
deliberately left. Each entry says what would make it worth doing.

Reviewed 2026-09-08, three adversarial rounds. Suite green: **349 passed, 16
skipped** (the skips are the `CROSSDROP_SMOKE=1` browser tests, which CI runs in
its own job). `ruff --select E9,F` and `shellcheck -S warning` clean.

Four rounds, the agreed cap. Round 1: 17 findings. Round 2: 9, including one
Critical. Round 3: 9, no Critical. Round 4: 12, one High, no Critical. Every
Critical, High and Medium was fixed, most with a test then shown to fail without
the fix.

The pattern is the reason four rounds were worth running: rounds 3 and 4 each
found their worst issue **in the previous round's fix**. Round 3's
`forget_targets()` closed a stale-cache hole single-threaded and left it open for
concurrent callers; round 4 closed that with a generation counter. Nothing in
round 4 was in code that predated this change.

**The stop rule was not met.** Round 4 still returned one High, so the criterion
("a round with zero Critical/High") never triggered — the 4-round cap did. A
fifth round would probably find something in round 4's locking. That is the
honest state of it, not a claim of completeness.

---

## Medium

### `swap_config` stops autoscroll on every settings save, not just a rename
`agent/app.py` — `swap_config`

The reason it exists is renames: `_autoscroll` is keyed by screen name, so a
rename orphans a running loop with nothing able to stop it. But it fires on any
save, so editing an unrelated `home_url` silently kills autoscroll on every
screen, and the 200 response says nothing about it.

**Why not now:** narrowing it means comparing old and new name lists and
stopping only the loops whose key moved — correct, but it adds a second place
that has to know how names map across a config swap, and getting *that* wrong
restores the haunted-display bug. Stopping everything is the conservative
direction: the failure is "your autoscroll stopped", not "your display scrolls
forever and only a restart fixes it".

**Do it when:** somebody actually uses autoscroll and the settings editor
together often enough to be annoyed. Add the note to `SettingsOut.note` first —
that is most of the complaint, and it is three lines.

### `setup.sh` destroys a pre-existing `video=` pin when `VIDEO=` is used
`deploy/pi/setup.sh` — the display-mode block

`-e '1s| video=[^ ]*||g'` strips every existing pin before appending ours, and
`$ETC/.video-pin` records only ours. So `uninstall.sh`'s promise that "any
`video=` pin this project did not place is also left alone" cannot be honoured
on a box where `VIDEO=` was used *and* a pin already existed.

**Why not now:** it needs the marker to record the *previous* value and the
uninstaller to restore it rather than strip it — a two-file change to an
interaction nobody has hit, on a path (`VIDEO=` explicitly set) that is already
documented as the unusual case.

**Do it when:** anyone reports a monitor coming up wrong after an uninstall.

### `migrate.sh` checks that it is not root, but not that `$HOME` owns the install
`deploy/pi/migrate.sh`

Run by a second sudo-capable admin account, `$OLD_DATA`, `$UNITS` and
`systemctl --user` all point at the wrong user while `sudo rm -rf "$OLD_OPT"`
still lands — leaving the kiosk user's frozen `display-agent.service` pointing
at a `WorkingDirectory` that no longer exists (200/CHDIR on every start) and
nothing new installed for them.

**Why not now:** the check is not obvious. "Owns the install" is really "is the
user the graphical session runs as", and the honest test for that is whether
`$UNITS/display-agent.service` exists — which is already a precondition in
spirit. Worth adding, but it is a new failure mode to get right on the one
script whose failures are physical trips.

**Do it when:** touching `migrate.sh` for any other reason. Add
`[ -f "$UNITS/display-agent.service" ]` to the precondition block at the top.

---

## Low — uncovered but deliberate

### Two defensive lines have no test that fails without them
- `browser.forget_targets()` clears `_guessed` as well as `_targets`. Deleting
  the `_guessed.clear()` leaves the suite green, because `_targets` is cleared
  with it and `_identify` re-derives the flag anyway. The line is correct and
  belt-and-braces; it is not load-bearing.
- `swap_config`'s *second* autoscroll sweep (the one after the swap, for names
  the new config no longer has) is likewise uncovered:
  `test_an_autoscroll_starting_during_a_swap_is_still_stopped` passes without it,
  because the pre-swap sweep already catches a start that has finished
  registering. Genuinely interleaving a start into the middle of a swap needs a
  seam inside `_autoscroll_start`, which is more test machinery than the risk
  justifies.

**Do it when:** either line is ever the suspect in a real incident. Both are
one-liners guarding a race, and the cost of keeping them is nil.

## Low — from round 3, not worth the churn

### `update.sh` reports "did not come up healthy at  within 30s" on the cannot-verify path
The message interpolates an empty `$BASE` and names a wait that never happened
(`seq 0`). The *branch* is right — it rolls back and, since round 3, does not
latch — only the sentence is wrong, and the line above it now says which of the
two reasons applied.

**Do it when:** anyone has to read that log for real.

### `update.sh`'s `.failed-*.jpg` screenshots are pruned only with their tag
The round-3 cleanup drops a marker once its release directory is gone, and takes
the `.jpg` with it. A failure snapshot for a tag whose directory was pruned
first survives until the next failure of the same tag.

**Do it when:** never, probably. It is one bounded jpeg.

## Low

### `tests/test_install_roundtrip.py` is slow
~110s for 25 tests, because each one forks ~40 stubs through git-bash. It is the
single slowest file in the suite by a wide margin.

**Why not now:** it is the only thing that proves install and uninstall round
trip, and every second of it is a real script running. Splitting it behind a
marker would make it skippable, which is how the CI smoke gap lasted before.

**Do it when:** it becomes the reason someone stops running the suite locally.
Then `-m "not slow"` for the inner loop and keep it unconditional in CI.

### The Windows tray app has no automated coverage
`deploy/windows/roomtray.ps1` — `Send-File`, `Poll`, `Send-Clipboard`, the menu
builder and `Save-Screen` are exercised by nothing. CI now parses both `.ps1`
files, so a syntax error is caught, but no behaviour is.

**Why not now:** `selfcheck.ps1` already extracts and tests the pure functions;
the rest needs a live agent and a Windows runner.

**Do it when:** the tray app changes. Until then the parse gate is the honest
level of assurance for a file nobody is editing.

---

## Accepted, not debt

These came up in review and are deliberate. Recorded so they are not re-raised.

- **`VERIFY_TAG` defaults to 0.** Turning tag signing on without a key in place
  stops every Pi updating, and a display stuck on an old release is worse than
  the risk it removes. The refusal path is tested; the accept path is a
  documented drill.
- **`_dedupe` renames rather than refuses.** Duplicates from `settings.json`
  must never stop a boot — the file is agent-written and outlives every
  restart. Duplicates in `config.toml` still raise, because a human with a
  keyboard wrote those.
- **`Status.up` is always `true` and `/v1/autoscroll` returns a screen name in
  `current_url`.** `/v1` is frozen; both are documented warts in README.
- **The round-trip test's `python3` stub fakes pip**, so it proves nothing about
  `--require-hashes`. CI's real install job is the verification, and the stub
  says so in a comment.
- **`/docs`, `/redoc` and `/openapi.json` are unauthenticated.** Checked
  deliberately: the schema they publish is the same `/v1` contract README
  documents in full, and `FastAPI(version="1")` means they carry the API
  version, not the release tag — so they leak nothing `/home-status` was
  trimmed to avoid. Every `/v1` route is behind `Depends(auth)`; the only other
  open routes are `/`, `/home`, `/home-status` and `/files/{id}`, each with a
  reason recorded at its definition.
