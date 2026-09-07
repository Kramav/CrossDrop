# Updating the Pi over SSH

A runbook for the box on the wall, from your desk. The *mechanism* — tags,
`selfcheck`, rollback, pruning — is [README.md §11](README.md); this is the
order you type things in.

The rule behind all of it: **the Pi has no keyboard.** Everything here either
leaves the display running or undoes itself, and nothing asks you to be in the
room.

---

## 1. Getting in

```sh
ssh room@<pi-tailnet-ip>          # or: tailscale ssh room@<hostname>
tailscale status | grep -i pi     # from your desk, if you forgot the address
```

**The one trap, first**, because everything below depends on it. The agent is a
systemd *user* unit, and an SSH session is not the graphical one:

```sh
systemctl --user status display-agent
```

If that says `Failed to connect to bus`, the session did not inherit the user
manager. Export it and it works for the rest of the session:

```sh
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
```

Put those two lines in `~/.bashrc` if you SSH in often. And note that `DISPLAY`
is **unset** over SSH — anything touching X (`xset`, `xrandr`, launching a
browser by hand) needs `DISPLAY=:0` in front of it.

---

## 2. Which update do you want

| You want | Go to |
|---|---|
| Ship code you have already tagged | §3 |
| Ship it *now*, not in ≤30 min | §4 |
| Retry a tag that rolled back | §5 |
| Change a config setting only | §6 |
| No tags yet — a plain checkout | §7 |

---

## 3. The normal path: cut a tag

Updates are **release-gated**. The Pi deploys the highest `v*` tag and nothing
else, so pushing to `main` changes nothing on the wall until you say so.

From your machine:

```sh
git push                                    # wait for CI green
git tag v1.3.0 && git push --tags
```

If `room-display-update.timer` is enabled the Pi picks it up within ~30 min
(`OnUnitActiveSec=30min`, plus up to 5 min of jitter). Check from your desk:

```sh
roomctl status | jq -r .version             # v1.3.0 once it has landed
```

Not enabled yet? That is deliberate — handing a Pi the right to replace its own
code is a decision, not a side effect of running `setup.sh`:

```sh
systemctl --user enable --now room-display-update.timer
systemctl --user list-timers room-display-update.timer
```

---

## 4. Don't wait for the timer

```sh
systemctl --user start room-display-update
journalctl --user -u room-display-update -f          # watch it decide
```

That is the same unit the timer runs, so it behaves identically — including
rolling itself back. It is safe to run when there is nothing to do: with no new
tag it prints `up to date (v1.3.0)` and writes nothing at all, which is what
keeps a 48×/day timer off the SD card.

> `room-display-update.service` runs `/opt/room-display/current/deploy/pi/update.sh`
> — the copy in the release that is **currently running**, not the one being
> deployed. So a change to `update.sh` itself only takes effect on the update
> *after* the one that ships it. Worth knowing when the thing you changed is the
> updater.

---

## 5. Retry a tag that rolled back

A tag that failed the live check is latched, so the timer stops re-deploying it
every 30 minutes and restarting the kiosk twice a cycle:

```sh
ls -a /opt/room-display/releases/ | grep failed
#  .failed-v1.3.0
#  .failed-v1.3.0.jpg      <- what the wall was showing when it failed
```

Copy the picture to your machine and look at it — that is usually the whole
diagnosis:

```sh
scp room@<pi>:/opt/room-display/releases/.failed-v1.3.0.jpg .
```

Then fix the cause, and clear the latch to let it try again:

```sh
rm /opt/room-display/releases/.failed-v1.3.0
systemctl --user start room-display-update
```

The broken release is left in `releases/<tag>/` on purpose, so you can read it.

---

## 6. Config-only change

`config.toml` holds the token and the install-time facts, and the agent
deliberately cannot write it (`root:<user> 640`). It needs `sudo` and a restart
— nothing here is picked up live.

```sh
sudo nano /etc/room-display/config.toml
systemctl --user restart display-agent
```

**Turning on typing** is this, and it is the one setting worth spelling out. Add:

```toml
[interact]
enabled = true
```

then restart, and confirm the agent is actually offering it:

```sh
roomctl status | jq -r '.supports | join(" ")'
#  navigate scroll autoscroll media screens window extensions screenshot inspect input
```

`input` present means clicking and typing are live. Absent means the block did
not take — check you edited the file `ROOM_CONFIG` points at
(`systemctl --user show display-agent -p Environment`).

Screen names, home URLs, positions and sizes are **not** here — those are the
web UI's Settings panel, they apply live, and they survive updates
(`~/.local/share/room-display/settings.json`).

---

## 7. A plain checkout, before there are any tags

`setup.sh` leaves `/opt/room-display/current` as a real git checkout. Until the
first tag is deployed there is nothing to roll back to, so this path has no
safety net — prefer §3 once you have tags.

```sh
git -C /opt/room-display/current pull --ff-only
/opt/room-display/current/.venv/bin/pip install -q -r \
    /opt/room-display/current/agent/requirements.txt
systemctl --user restart display-agent
```

Check it can even start **before** you restart, while the running agent is still
up. This is the same gate `update.sh` uses, it binds no port and launches no
browser, so it is safe with the kiosk live:

```sh
cd /opt/room-display/current \
  && ROOM_CONFIG=/etc/room-display/config.toml .venv/bin/python -m agent selfcheck
```

Exit 0 means it loads, imports and answers `/v1/status`. Exit 1 means **do not
restart** — you would be trading a working display for a boot loop.

---

## 8. Did it work

From your desk, not from the Pi — the point is that it answers over the tailnet:

```sh
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

**The full acceptance run**, on the Pi, with a real browser. This is what proves
screenshots are in the right coordinate space and that a click lands where it
was aimed:

```sh
cd /opt/room-display/current
ROOM_BROWSER=chromium ROOM_SMOKE=1 .venv/bin/python -m pytest tests/test_smoke.py -q
```

It opens one kiosk window over whatever is on the wall for about half a minute,
then puts it back. Run it when nobody is using the room.

---

## 9. Rolling back by hand

`update.sh` does this itself on a failed deploy. Do it manually when a release
came up *healthy* but is wrong — a bad home page, a broken layout — which no
automatic check can catch.

```sh
ls -1 /opt/room-display/releases/          # what is available
readlink /opt/room-display/current         # what is running

ln -sfn /opt/room-display/releases/v1.2.0 /opt/room-display/current
systemctl --user restart display-agent
roomctl status | jq -r .version
```

Then stop the timer putting the bad one straight back:

```sh
systemctl --user stop room-display-update.timer
touch /opt/room-display/releases/.failed-v1.3.0     # or delete the tag upstream
```

Updates swap **code only** — the symlink move cannot touch your token, your
logins or your screen settings. Those live outside the release tree:

    /etc/room-display/config.toml                       token, install-time facts
    ~/.local/share/room-display/settings.json           screens, home urls
    ~/.local/share/room-display/profile.tar.gz          the logins
    /opt/room-display/extensions/                       ad blockers

> **The paths say `room-display`, the repo says CrossDrop.** That is deliberate
> and settled — CrossDrop is the source, `room-display` is the installation. See
> PLAN.md §11 "Naming". The data dir is the one you can move, with `ROOM_DATA`
> in `display-agent.service`; it moves the snapshot script with it.

---

## 10. When the wall is wrong but SSH works

```sh
journalctl --user -u display-agent -n 100 --no-pager      # this boot
journalctl --user -u display-agent -f                     # follow
journalctl --user -u display-agent | grep /v1/            # what was asked of it
systemctl --user list-units --failed
```

Every request is logged: `POST /v1/navigate -> 200 in 41ms`. Mutations at INFO,
reads at DEBUG — add `Environment=ROOM_LOG=DEBUG` to the unit for the polls too.

**The journal is in RAM and does not survive a reboot**
([journald-volatile.conf](journald-volatile.conf)) — read it *before* you reboot
the box, or you lose the evidence.

Things worth trying before a restart, all from your desk:

```sh
roomctl inspect | jq '{title, error_page, url}'   # is it a crash page?
roomctl reload                                    # re-navigate
roomctl home
roomctl window minimized                          # frees the Pi's desktop
roomctl window fullscreen                         # and puts the kiosk back
```

A restart of the agent relaunches the browser and is nearly always the fix for a
wedged kiosk. It costs the current page, and any running autoscroll:

```sh
systemctl --user restart display-agent
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
- **Don't `systemctl stop display-agent` and walk away.** That leaves the
  monitors with no one to wake them — `roomctl window minimized` is the way to
  free the desktop without giving up display power.
- **Don't reboot to read logs.** They are in RAM. See §10.
