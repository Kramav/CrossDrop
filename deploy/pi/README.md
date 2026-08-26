# Phase 5 — Pi provisioning

Gets the agent running on the Pi under the autologin desktop session, with a
persistent (on-SD) browser profile. RAM profile + auth snapshotting is Phase 6.

Paths match PLAN.md §8, so Phase 8 only has to repoint the `current` symlink —
nothing here changes when auto-update lands.

    /opt/room-display/current          code + venv   (a plain dir now, a symlink in Phase 8)
    /etc/room-display/config.toml      token, paths  (never overwritten by updates)
    ~/.local/share/room-display/       browser profile (persistent, Phase 5)
    /run/user/1000/room-display/       uploads — already tmpfs, so already RAM

**Just want it running?** [setup.sh](setup.sh) does this page *and* pi-setup.md
§2-§7 in one go, on a card you've already imaged:

```sh
curl -fsSL https://raw.githubusercontent.com/Kramav/CrossDrop/main/deploy/pi/setup.sh | bash
```

The rest of this page is what it does, for when a step needs debugging.

## 1. A configured Pi

[pi-setup.md](pi-setup.md) first — imaging, SSH, Tailscale, desktop autologin,
screen blanking off, display mode pinned. Its step 7 is the checklist that says
you're ready for this page.

## 2. Packages

```sh
sudo apt update
sudo apt install -y chromium-browser python3-venv git
```

Tailscale should already be up (`tailscale ip -4` prints a 100.x address).

## 3. Code

```sh
sudo mkdir -p /opt/room-display
sudo chown "$USER" /opt/room-display
git clone <repo> /opt/room-display/current
cd /opt/room-display/current
python3 -m venv .venv
.venv/bin/pip install -r agent/requirements.txt
```

## 4. Config

```sh
sudo mkdir -p /etc/room-display
sudo cp /opt/room-display/current/agent/config.example.toml /etc/room-display/config.toml
sudo nano /etc/room-display/config.toml
```

Pi values — the rest of the file is fine as shipped:

```toml
token = "<a long random string>"
home_url = "about:blank"

[browser]
kind = "chromium"
profile_dir = "/home/<user>/.local/share/room-display/profile"

[upload]
dir = "/run/user/1000/room-display/uploads"
```

`/run/user/1000` is tmpfs, so uploads are in RAM with no setup — that is the
uploads half of Phase 6 already done. `max_mb` × `keep` is the RAM ceiling:
the shipped 25 × 20 can reach 500 MB, so lower `keep` on a 2 GB Pi.

The token is a secret, and the file is world-readable by default:

```sh
sudo chown root:"$USER" /etc/room-display/config.toml
sudo chmod 640 /etc/room-display/config.toml
```

## 5. Service

```sh
mkdir -p ~/.config/systemd/user
cp /opt/room-display/current/deploy/pi/display-agent.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now display-agent
```

No `kiosk-launch.sh`: the agent launches and owns the kiosk itself, so a
separate launcher would just be a second thing to keep in sync.

## 6. Acceptance (PLAN.md §7 Phase 5)

```sh
sudo reboot
```

Desktop autologins, kiosk comes up, and from the Win10 desktop or Win11 laptop —
nobody at the Pi:

```sh
curl -H "Authorization: Bearer <token>" http://<pi-tailnet-ip>:8080/v1/status
curl -X POST -H "Authorization: Bearer <token>" -H 'Content-Type: application/json' \
     -d '{"url":"https://example.com"}' http://<pi-tailnet-ip>:8080/v1/navigate
```

The screen should change. Then open `http://<pi-tailnet-ip>:8080/` in an app
window for the drop-zone UI:

```
msedge.exe --app=http://<pi-tailnet-ip>:8080/
```

## Troubleshooting

`journalctl --user -u display-agent -f` is the log.

- **Restart loop, "debug port 9222 never came up"** — the compositor was not up
  yet; systemd retries every 5 s, so give it a minute after boot. If it never
  settles, the unit's `DISPLAY`/`WAYLAND_DISPLAY` don't match the session —
  read the real values with the recipe in [pi-setup.md](pi-setup.md) §6.
- **`tailscale ip -4` fails in the unit** — your user cannot query tailscaled.
  Put the literal 100.x address in `ExecStart` instead.
- **Agent starts but the screen never changes** — something else already holds
  9222 and the agent is driving *that* browser. `pgrep -a chromium`; kill the
  strays and restart. (This is why PLAN.md §6 wants the profile dir dedicated.)
- **Dropped files 404 on the display** — the browser is fetching a hostname the
  Pi itself cannot resolve. Reach the agent by tailnet IP or MagicDNS name.
