# A Debian box as the display (Proxmox host)

Same agent, same `/v1` API, same `roomctl` and tray client. The only thing that
changes from the Pi is how the graphical session gets built, and
[pi/setup.sh](pi/setup.sh) now does both — it detects `raspi-config` and takes
the other branch when it isn't there.

## Run it on the host, not in a guest

The agent has to reach a **physical monitor**, and on a hypervisor only the host
owns the GPU. So:

| | Verdict |
|---|---|
| **On the PVE host** | What this page does. The iGPU is already there and idle, no passthrough, nothing to break at boot. Costs ~250 MB of Xorg + Chromium on the host. |
| LXC with `/dev/dri` bound in | Needs a privileged container plus DRM/tty access, which is most of the host's authority anyway — fiddly setup for isolation it doesn't really give you. |
| VM with GPU passthrough | Needs IOMMU, VFIO and a GPU the host can give up entirely. On a single-iGPU desktop box the host loses its console. Far more machinery for the same picture on the wall. |

A room display is not the workload to build a hypervisor boundary around: it
renders web pages you already trust enough to put on a wall.

## Install

Proxmox gives you root and nothing else, but Chromium refuses to run as root and
the agent runs as whoever owns the session. Make a user first:

```sh
adduser --gecos "" room
usermod -aG sudo,video,render,input room
su - room
curl -fsSL https://raw.githubusercontent.com/Kramav/CrossDrop/main/deploy/pi/setup.sh | bash
sudo reboot
```

It prints the token and a `curl` to try from another box. Then, from a
controller machine:

```sh
roomctl -t <name> status
```

## What it set up

Not a desktop — the smallest session that satisfies the agent:

    /etc/systemd/system/getty@tty1.service.d/autologin.conf   agetty autologins `room` on tty1
    ~/.profile (or ~/.bash_profile)                           that login runs `startx -- -nocursor`
    ~/.xinitrc                                                `exec openbox-session`
    ~/.config/systemd/user/display-agent.service              the agent, started by the same login

X11 rather than Wayland on purpose: monitor power in
[agent/display.py](../agent/display.py) is `xset`/DPMS, and a keyboard-less
display that blanks with nothing able to wake it is the bug this project exists
to avoid. `openbox` is there because placing and fullscreening one window per
monitor needs a window manager; nothing else uses it.

Multi-monitor needs no config — the agent reads `xrandr --listmonitors` and
makes a screen per connected output. Rename them in the web UI's **Settings**.

## Proxmox-specific notes

- **`apt update` is allowed to fail.** A host with no subscription 401s on the
  enterprise repo; every package the script installs comes from Debian main.
- **No `apt full-upgrade`.** The script runs one on a Pi and skips it here —
  upgrading a hypervisor's kernel under its VMs is your call, not a side effect
  of installing a kiosk.
- **Logs stay on disk.** The Pi moves journald to RAM to spare the SD card; an
  SSD doesn't need it, so the host keeps a persistent journal.
- **Don't also pass the GPU to a VM.** Xorg on the host and VFIO want the same
  device.
- Tailscale on the host is what the agent binds to (`display-agent.service`
  asks `tailscale ip -4`), so the API is never on the LAN.

## Backing it out

```sh
systemctl --user disable --now display-agent
sudo rm -rf /etc/systemd/system/getty@tty1.service.d /opt/room-display /etc/room-display
sudo systemctl daemon-reload
sudo apt purge -y chromium xserver-xorg xinit x11-xserver-utils openbox && sudo apt autoremove -y
```
