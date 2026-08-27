# CrossDrop

Drive a keyboard-less Raspberry Pi room display from any box on the tailnet:
point it at a URL, or drop a file on it and have it render.

- **Pi setup** — [deploy/pi/pi-setup.md](deploy/pi/pi-setup.md) (OS) then
  [deploy/pi/README.md](deploy/pi/README.md) (agent). One script does both:
  [deploy/pi/setup.sh](deploy/pi/setup.sh).
- **Build plan and phases** — [PLAN.md](PLAN.md).

## Controlling a display

Two ways, same frozen API underneath.

**Web UI** — the drop zone the agent serves. Open it as its own window:

```powershell
msedge.exe --app=http://<pi-tailnet-ip>:8080/
```

Paste a link or drag a file (`.pdf .png .jpg .jpeg .gif .webp .txt`, 25 MB cap)
and the display follows. It asks for the agent token once and remembers it.

**`roomctl`** — the CLI, and the same functions eve imports.

```sh
pip install -e .                                  # puts `roomctl` on PATH
cp roomctl/targets.example.toml roomctl/targets.toml
$EDITOR roomctl/targets.toml                      # the Pi's 100.x address + token

roomctl status
roomctl navigate https://example.com
roomctl upload ~/slides.pdf
roomctl home
roomctl -t spare status                           # a second display

roomctl screens                                   # what monitors this Pi has
roomctl navigate https://example.com -s right     # one monitor
roomctl navigate https://example.com -s all       # both
roomctl scroll --down | --up | --top | --bottom
roomctl autoscroll start -s right --speed 60
```

A **target** is a Pi; a **screen** is one monitor on it. Omit `-s` and you get
the first screen, which is the whole story on a single-monitor display.

Every command prints the agent's JSON reply, so it pipes into `jq`; errors go to
stderr with exit 1. `targets.toml` holds bearer tokens and is git-ignored.
`ROOMCTL_TARGETS` overrides its location. No install needed on a box that just
has the checkout: `python -m roomctl status`.

## The `/v1` contract

Frozen. Bearer token on every route; FastAPI publishes the schema at `/docs`.

Every route takes an optional `screen` (a name, or `"all"`). Omitted means the
first screen — multi-monitor was added by *adding* a field, so a client written
before it keeps working unchanged.

| Route | Body | Returns |
|---|---|---|
| `POST /v1/navigate` | `{"url": "...", "screen"?}` | `{"ok": true, "current_url": "..."}` |
| `POST /v1/upload` | multipart `file`, `screen`? | `{"id": "...", "url": "/files/<id>"}`, then auto-navigates |
| `POST /v1/reload` | `{"screen"?}` | `{"ok": true, "current_url": "..."}` |
| `POST /v1/home` | `{"screen"?}` | `{"ok": true, "current_url": "..."}` |
| `POST /v1/scroll` | `{"screen"?, "dy"?, "to"?}` | `to` is `"top"`\|`"bottom"`; else `dy` pixels |
| `POST /v1/autoscroll` | `{"screen"?, "action", "speed"?}` | `action` is `"start"`\|`"stop"` |
| `GET /v1/screens` | — | `[{"name", "position", "current_url", "autoscroll"}]` |
| `GET /v1/status` | — | as before, plus `"screens": [...]` |

`GET /home` is the idle screen the kiosk sits on, and `GET /home-status` feeds
it. Both are unauthenticated for the same reason `/files` is — the kiosk browser
can't send a header. `/home-status` reports the **host** of what other screens
are showing, never the full URL.

`GET /files/{id}` is unauthenticated on purpose — the kiosk browser fetches it
and cannot send a header. The random id is the capability, and ids are never
listed.

## Tests

```sh
pytest                  # no browser needed
ROOM_SMOKE=1 pytest -s  # drives a real kiosk browser
```
