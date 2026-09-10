# Updating the Pi over SSH

> **This runbook spans two machines.** Every block below starts with a comment
> saying which one, so no block depends on the one before it:
>
> - `# on the Pi` — over SSH, as the user that owns the graphical session. Any
>   directory: those blocks use absolute paths or carry their own `cd`.
> - `# on your machine` — your desk. `roomctl` and `scp` work from any
>   directory; `git` (§3) needs your **CrossDrop clone**, so those blocks carry a
>   `cd`.
>
> `roomctl` exists only on your machine — it is the desk-side client, installed
> with `pip install -e .` ([README.md, "Controlling a display"](../../README.md)).
> Activate that venv first if it is in one.
>
> The Pi's own layout, for reading the blocks below:
>
> | Path | What lives there |
> |---|---|
> | `/opt/crossdrop/current` | the code — a symlink to the running release |
> | `/opt/crossdrop/releases/` | past releases, and the `.failed-*` markers |
> | `/etc/crossdrop/config.toml` | the token and install-time facts, root-owned |
> | `~/.local/share/crossdrop/` | screen settings, and the profile snapshot |

A runbook for the box on the wall, from your desk. The *mechanism* — tags,
`selfcheck`, rollback, pruning — is [README.md §11](README.md); this is the
order you type things in.

**Start at [§2](#2-which-update-do-you-want)**, the index. It will tell you
which of the paths below you are on — and the ordinary one, §3, needs no SSH at
all: you tag on your machine and the Pi comes and gets it.

The rule behind all of it: **the Pi has no keyboard.** Everything here either
leaves the display running or undoes itself, and nothing asks you to be in the
room.

Three words used throughout: a **tag** is what you push (`v1.3.0`); a
**release** is the unpacked copy of that tag in `releases/v1.3.0/`; **current**
is the symlink saying which release is running. Deploying is moving that
symlink, and rolling back is moving it back.

---

## 1. Getting in

```sh
# on your machine — any directory
tailscale status | grep -i pi     # if you forgot the address
ssh room@<pi-tailnet-ip>          # or: tailscale ssh room@<hostname>
```

**The one trap, first**, because everything below depends on it. The agent is a
systemd *user* unit, and an SSH session is not the graphical one:

```sh
# on the Pi
systemctl --user status crossdrop-agent
```

If that says `Failed to connect to bus`, the session did not inherit the user
manager. Export it and it works for the rest of the session:

```sh
# on the Pi
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
```

Put those two lines in `~/.bashrc` if you SSH in often. And note that `DISPLAY`
is **unset** over SSH — anything touching X (`xset`, `xrandr`, launching a
browser by hand) needs `DISPLAY=:0` in front of it.

---

## 2. Which update do you want

| You want | Go to | SSH? |
|---|---|---|
| Ship code — the ordinary path | §3 | no |
| Ship it now, without waiting ~30 min for the timer | §4 | yes |
| Retry a tag that rolled back | §5 | yes |
| Change a config setting only | §6 | yes |
| No tags yet — a plain checkout | §7 | yes |
| Put an older release back | §9 | yes |
| The wall is wrong and you want to know why | §10 | yes |
| Start over — wipe and reinstall | [README.md §13](README.md#13-uninstall-or-wipe-and-reinstall) | yes |

---

## 3. The normal path: cut a tag

Updates are **release-gated**. The Pi deploys the highest `v*` tag and nothing
else, so pushing to `main` changes nothing on the wall until you say so.

```sh
# on your machine, from your CrossDrop clone
cd ~/src/CrossDrop                          # wherever you cloned it
git push                                    # wait for CI green
git tag v1.3.0 && git push --tags
```

That is the whole of the normal path — you never touch the Pi. If
`crossdrop-update.timer` is enabled it picks the tag up within ~30 min
(`OnUnitActiveSec=30min`, plus up to 5 min of jitter), so wait, then check it
landed:

```sh
# on your machine — any directory
roomctl status | jq -r .version             # v1.3.0 once it has landed
```

Not enabled yet? That is deliberate — handing a Pi the right to replace its own
code is a decision, not a side effect of running `setup.sh`:

```sh
# on the Pi
systemctl --user enable --now crossdrop-update.timer
systemctl --user list-timers crossdrop-update.timer
```

---

## 4. Don't wait for the timer

```sh
# on the Pi
systemctl --user start crossdrop-update
journalctl --user -u crossdrop-update -f          # watch it decide
```

That is the same unit the timer runs, so it behaves identically — including
rolling itself back. It is safe to run when there is nothing to do: with no new
tag it prints `up to date (v1.3.0)` and writes nothing at all, which is what
keeps a 48×/day timer off the SD card.

> `crossdrop-update.service` runs `/opt/crossdrop/current/deploy/pi/update.sh`
> — the copy in the release that is **currently running**, not the one being
> deployed. So a change to `update.sh` itself only takes effect on the update
> *after* the one that ships it. Worth knowing when the thing you changed is the
> updater.

---

## 5. Retry a tag that rolled back

A tag that failed the live check is latched, so the timer stops re-deploying it
every 30 minutes and restarting the kiosk twice a cycle:

```sh
# on the Pi
ls -a /opt/crossdrop/releases/ | grep failed
#  .failed-v1.3.0
#  .failed-v1.3.0.jpg      <- what the wall was showing when it failed
```

Copy the picture to your machine and look at it — that is usually the whole
diagnosis:

```sh
# on your machine — the .jpg lands in the directory you run this from
cd ~/Downloads
scp room@<pi-tailnet-ip>:/opt/crossdrop/releases/.failed-v1.3.0.jpg .
```

Then fix the cause, and clear the latch to let it try again:

```sh
# on the Pi
rm /opt/crossdrop/releases/.failed-v1.3.0
systemctl --user start crossdrop-update
```

The broken release is left in `releases/<tag>/` on purpose, so you can read it.

---

## 6. Config-only change

`config.toml` holds the token and the install-time facts, and the agent
deliberately cannot write it (`root:<user> 640`). It needs `sudo`.

```sh
# on the Pi — absolute path, any directory
sudo nano /etc/crossdrop/config.toml
```

The agent re-reads the file within a few seconds of a save, so the token, the
`[[screen]]` blocks and `[interact]` take effect on their own. `[browser]`
settings — `kind`, `profile_dir`, `debug_port`, `extensions_dir` — do **not**:
the browser is already running with them, so those still want a restart:

```sh
# on the Pi
systemctl --user restart crossdrop-agent
```

A malformed edit costs you the edit, not the display: the agent logs why and
goes on serving the config it already has until the file parses again. Which
file it loaded, and how long the token in it is, are the first thing it prints —
`config: /etc/crossdrop/config.toml (token 64 chars)`. A length that is not 64
is a token that got wrapped or truncated by an editor, which reads as "my token
stopped working".

**Turning on typing** is this, and it is the one setting worth spelling out. Add:

```toml
[interact]
enabled = true
```

then restart, and confirm the agent is actually offering it:

```sh
# on your machine — any directory
roomctl status | jq -r '.supports | join(" ")'
#  navigate scroll autoscroll media screens window extensions screenshot inspect input
```

`input` present means clicking and typing are live. Absent means the block did
not take — check you edited the file `CROSSDROP_CONFIG` points at
(`systemctl --user show crossdrop-agent -p Environment`).

Screen names and home URLs are **not** here — those are the web UI's Settings
panel, they apply live, and they survive updates
(`~/.local/share/crossdrop/settings.json`). Positions and sizes are not stored
anywhere: the agent reads them from `xrandr` every time it loads. `[[screen]]`
blocks in this file can still pin them, and a stale pin outranks the monitors
you actually have — check for one here before believing a layout is wrong.

---

## 7. A plain checkout, before there are any tags

`setup.sh` leaves `/opt/crossdrop/current` as a real git checkout. Until the
first tag is deployed there is nothing to roll back to, so this path has no
safety net — prefer §3 once you have tags.

Three steps, in this order. **Pull and install first** — neither restarts
anything, so the agent on the wall keeps serving from the code it already
loaded:

```sh
# on the Pi — absolute paths, any directory
git -C /opt/crossdrop/current pull --ff-only
/opt/crossdrop/current/.venv/bin/pip install -q -r \
    /opt/crossdrop/current/agent/requirements.txt
```

**Then check the new code can even start**, while the old one is still up. This
is the same gate `update.sh` uses; it binds no port and launches no browser, so
it is safe with the kiosk live:

```sh
# on the Pi
cd /opt/crossdrop/current \
  && CROSSDROP_CONFIG=/etc/crossdrop/config.toml .venv/bin/python -m agent selfcheck
```

Exit 0 means it loads, imports and answers `/v1/status`. Exit 1 means **stop
here** — restarting would trade a working display for a boot loop. The agent on
the wall is still the old code, so nothing is broken yet; undo the pull with
`git -C /opt/crossdrop/current reset --hard HEAD@{1}` and leave it running.

**Only then restart**, which is the moment the wall actually changes:

```sh
# on the Pi
systemctl --user restart crossdrop-agent
```

---

## 8. Did it work

From your desk, not from the Pi — the point is that it answers over the tailnet:

```sh
# on your machine — any directory; wall.png lands in it
roomctl status | jq '{version, browser, error, awake}'
roomctl shot -o wall.png                 # and actually look at it
```

`error` is empty when all is well. If the browser failed to launch, the agent
**stays up and says why** rather than dying, so this is where the answer is:

```json
{"version": "v1.3.0", "browser": "down",
 "error": "RuntimeError: no chromium binary found; set browser.path in config"}
```

It keeps retrying in the background (5 s, doubling to 5 min), so a transient
cause — the compositor was not up yet — clears itself and `error` goes back to
`""` with no restart.

**The full acceptance run**, on the Pi, with a real browser — what proves
captures are in the right coordinate space and that a click lands where it was
aimed. **Stop the agent first**; it holds the debug port the tests need, and
they refuse to start otherwise:

```sh
# on the Pi — run from /opt/crossdrop/current, not /etc/crossdrop
systemctl --user stop crossdrop-agent
cd /opt/crossdrop/current
DISPLAY=:0 CROSSDROP_BROWSER=chromium CROSSDROP_SMOKE=1 \
  .venv/bin/python -m pytest tests/test_smoke.py -q
systemctl --user start crossdrop-agent
```

14 passed, about 20 seconds, one fullscreen window that closes itself. The wall
comes back to what it was showing. Full walkthrough, including what each test
proves and what to do when one fails:
[smoke-on-the-pi.md](smoke-on-the-pi.md).

---

## 9. Rolling back by hand

`update.sh` does this itself on a failed deploy. Do it manually when a release
came up *healthy* but is wrong — a bad home page, a broken layout — which no
automatic check can catch.

```sh
# on the Pi — absolute paths, any directory. Do NOT cd into `current` first:
# it is the symlink you are about to move out from under yourself.
ls -1 /opt/crossdrop/releases/          # what is available
readlink /opt/crossdrop/current         # what is running

ln -sfn /opt/crossdrop/releases/v1.2.0 /opt/crossdrop/current
systemctl --user restart crossdrop-agent
```

Confirm the old version is the one answering:

```sh
# on your machine — any directory
roomctl status | jq -r .version            # v1.2.0
```

Then stop the timer putting the bad one straight back:

```sh
# on the Pi
systemctl --user stop crossdrop-update.timer
touch /opt/crossdrop/releases/.failed-v1.3.0     # or delete the tag upstream
```

Updates swap **code only** — the symlink move cannot touch your token, your
logins or your screen settings. Those live outside the release tree:

    /etc/crossdrop/config.toml                       token, install-time facts
    ~/.local/share/crossdrop/settings.json           screens, home urls
    ~/.local/share/crossdrop/profile.tar.gz          the logins
    /opt/crossdrop/extensions/                       ad blockers

> **The paths say `crossdrop`, the repo says CrossDrop.** That is deliberate
> and settled — CrossDrop is the source, `crossdrop` is the installation. See
> PLAN.md §11 "Naming". The data dir is the one you can move, with `CROSSDROP_DATA`
> in `crossdrop-agent.service`; it moves the snapshot script with it.

---

## 10. When the wall is wrong but SSH works

```sh
# on the Pi — any directory
journalctl --user -u crossdrop-agent -n 100 --no-pager      # this boot
journalctl --user -u crossdrop-agent -f                     # follow
journalctl --user -u crossdrop-agent | grep /v1/            # what was asked of it
systemctl --user list-units --failed
```

Every request is logged: `POST /v1/navigate -> 200 in 41ms`. Mutations at INFO,
reads at DEBUG — add `Environment=CROSSDROP_LOG=DEBUG` to the unit for the polls too.

**The journal is in RAM and does not survive a reboot**
([journald-volatile.conf](journald-volatile.conf)) — read it *before* you reboot
the box, or you lose the evidence.

Things worth trying before a restart, all from your desk:

```sh
# on your machine — any directory
roomctl inspect | jq '{title, error_page, url}'   # is it a crash page?
roomctl reload                                    # re-navigate
roomctl home
roomctl window minimized                          # frees the Pi's desktop
roomctl window fullscreen                         # and puts the kiosk back
```

A restart of the agent relaunches the browser and is nearly always the fix for a
wedged kiosk. It costs the current page, and any running autoscroll:

```sh
# on the Pi
systemctl --user restart crossdrop-agent
```

The Pi does this to itself nightly at 04:00 anyway (README.md §6), which is why
what you left on screen comes back afterwards.

---

## Don't

- **Don't edit files under `releases/<tag>/`.** The next update replaces that
  directory wholesale and your change disappears without a trace. Change the
  repo, tag it.
- **Don't make `config.toml` agent-writable.** It holds the bearer token; the
  `640` and the root ownership are the reason an API that can install browser
  extensions still cannot rewrite its own credentials.
- **Don't run the agent as root.** Chromium refuses to start, and it needs to be
  the user who owns the graphical session anyway.
- **Don't `systemctl stop crossdrop-agent` and walk away.** That leaves the
  monitors with no one to wake them — `roomctl window minimized` is the way to
  free the desktop without giving up display power.
- **Don't reboot to read logs.** They are in RAM. See §10.
