# Configuring a Pi for the room display

OS-level setup, from a blank SD card to a box ready for the agent. Installing
the agent itself is [README.md](README.md) — do that after this.

The governing constraint: **the display Pi has no keyboard and no mouse.** Every
setting here either has to be pre-seeded onto the card or done over SSH. If a
step needs a keyboard at the Pi, it is the wrong step.

You have two Pis (`rpi4b`, `rpi54gb`). Either works. Doing both is worth it: the
spare becomes the box you test a release tag on before cutting it, which is
what makes Phase 8's auto-update safe to leave unattended.

Step 1 is the only part that needs a human at a Windows box. Everything after it
— and all of README.md — is [setup.sh](setup.sh), run once over SSH:

```sh
curl -fsSL https://raw.githubusercontent.com/Kramav/CrossDrop/main/deploy/pi/setup.sh | bash
```

It pauses once, for the Tailscale auth URL. Read on if a step needs debugging,
or if your screen isn't 1920x1080 (`VIDEO=1280x720@60D bash setup.sh`).

---

## 1. Image the card

Use **Raspberry Pi Imager** on the Windows box. Choose Raspberry Pi OS (64-bit)
**with desktop** — not Lite. The kiosk needs a graphical session, and 64-bit
gives aarch64 wheels so pydantic-core installs without a compiler (assumptions
A4/A5 in PLAN.md).

Before writing, open **OS Customisation** and set all of it:

| Setting | Value |
|---|---|
| Hostname | `rpi54gb` (or whichever you're using) |
| Username / password | your usual Pi user — note the name, the agent runs as it |
| Wi-Fi SSID / password / country | the study network |
| Locale / timezone / keyboard | yours |
| Services → **Enable SSH** | **Use public-key authentication** |

This is the whole point of the Imager: a Pi that joins the network and accepts
your SSH key on first boot, with nobody typing anything at it. Skipping this
step is what forces you to go find a keyboard.

## 2. First boot

Card in, HDMI in, power on. Give it a couple of minutes, then from the desktop:

```sh
ssh <user>@<hostname>.local
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

If `.local` doesn't resolve, find the Pi's address in your router's client list.

## 3. Tailscale

This is how both controllers reach the Pi, and how you'll reach it once it's
behind the study's network (assumption A3).

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
```

Open the printed URL to authenticate. Then confirm — this address is what goes
in the agent's URL and what the service binds to:

```sh
tailscale ip -4
```

Two things worth doing in the Tailscale admin console while you're there:

- **Disable key expiry** for this node. The default expires the key every ~6
  months, and when it does, a keyboard-less Pi drops off the tailnet with no way
  to re-auth it except plugging in a keyboard.
- Confirm `tailscale ip -4` works **without sudo** as your user. The service unit
  calls it to find its bind address. If it needs root, put the literal 100.x
  address in `ExecStart` instead.

## 4. raspi-config

```sh
sudo raspi-config
```

Four settings, all required:

| Menu | Setting | Why |
|---|---|---|
| System Options → Boot / Auto Login | **Desktop Autologin** | the kiosk needs a graphical session at boot with nobody present |
| Display Options → **Screen Blanking** | **No** | otherwise the display sleeps after 10 minutes and the whole project looks broken |
| Display Options → Wayland | **labwc** (the default) | what the service unit's environment assumes |
| Advanced Options → Wayland/GL | leave alone | the defaults are right on Pi 4 and 5 |

Screen Blanking is the one people miss. It is a kiosk display; it must never
sleep. `raspi-config` sets this correctly on both labwc and X11, which is why
it's the setting to use rather than editing a compositor config by hand.

Reboot and confirm it comes back to a logged-in desktop by itself:

```sh
sudo reboot
```

## 5. Pin the display mode

A Pi with no monitor attached at boot — or one whose screen powers on after the
Pi does — comes up with no display detected and the kiosk lands on a 640×480
phantom output. Pin the mode in firmware so it never depends on what the screen
reports at the moment of boot:

```sh
sudo nano /boot/firmware/cmdline.txt
```

Append to the **single existing line** (it must stay one line):

```
video=HDMI-A-1:1920x1080@60D
```

The trailing `D` forces the output on whether or not a monitor answers. Match
the resolution to the actual screen.

After a reboot, check what you actually got:

```sh
wlr-randr          # labwc / Wayland
```

## 6. Check what the session actually exports

The service unit sets `DISPLAY=:0` and `WAYLAND_DISPLAY=wayland-0`. Those are
the usual values, but confirm them against the real session rather than trusting
it — a mismatch here is the difference between a working kiosk and a service
that restarts forever.

`echo $WAYLAND_DISPLAY` **over SSH will be empty**, because your SSH session
isn't the graphical one. Read it from the desktop session's own process:

```sh
pgrep -u "$USER" labwc                     # the compositor's pid
tr '\0' '\n' < /proc/$(pgrep -u "$USER" labwc | head -1)/environ \
  | grep -E 'WAYLAND_DISPLAY|DISPLAY|XDG_RUNTIME_DIR'
```

If `WAYLAND_DISPLAY` is anything other than `wayland-0`, edit that line in
`~/.config/systemd/user/display-agent.service` to match.

`XDG_RUNTIME_DIR` should be `/run/user/1000`. If your user isn't uid 1000,
adjust the `upload.dir` path in the agent config accordingly — check with `id -u`.

## 7. Sanity check before installing the agent

```sh
id -u                      # expect 1000; upload.dir in the config uses it
tailscale ip -4            # expect 100.x, no sudo
findmnt /run/user/$(id -u) # expect tmpfs — this is what makes uploads RAM-only
chromium-browser --version # expect a version; installed in README.md step 2
```

If all four are good, go to [README.md](README.md) and install the agent.

---

## Deliberately not done here

- **No static IP.** Tailscale gives the Pi a stable 100.x address that works
  from anywhere; a LAN static IP is a second address to keep in sync.
- **No log2ram, no tmpfs profile.** That's Phase 6. Phase 5 runs a persistent
  on-SD profile on purpose, to isolate variables — get the kiosk working first,
  then make it stop writing to the card.
- **No unattended-upgrades change.** Updates to *this project* are release-gated
  in Phase 8; OS package updates are a separate concern, left at the default.
- **No VNC.** `tailscale up --ssh` already gives you a way in. Turn it on only
  if you hit something that genuinely needs eyes on the desktop.

## References

- [Raspberry Pi OS release notes — labwc as the default compositor](https://www.raspberrypi.com/news/a-new-release-of-raspberry-pi-os/)
- [raspi-config guide — Display Options / Screen Blanking](https://raspberrytips.com/raspi-config-guide/)
- [Disabling screen blanking on Raspberry Pi](https://pimylifeup.com/raspberry-pi-disable-screen-blanking/)
