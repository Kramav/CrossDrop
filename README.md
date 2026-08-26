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
```

Every command prints the agent's JSON reply, so it pipes into `jq`; errors go to
stderr with exit 1. `targets.toml` holds bearer tokens and is git-ignored.
`ROOMCTL_TARGETS` overrides its location. No install needed on a box that just
has the checkout: `python -m roomctl status`.

## The `/v1` contract

Frozen. Bearer token on every route; FastAPI publishes the schema at `/docs`.

| Route | Body | Returns |
|---|---|---|
| `POST /v1/navigate` | `{"url": "..."}` | `{"ok": true, "current_url": "..."}` |
| `POST /v1/upload` | multipart `file` | `{"id": "...", "url": "/files/<id>"}`, then auto-navigates |
| `POST /v1/reload` | — | `{"ok": true, "current_url": "..."}` |
| `POST /v1/home` | — | `{"ok": true, "current_url": "..."}` |
| `GET /v1/status` | — | `{"up": true, "current_url": "...", "browser": "ok", "version": "<tag>"}` |

`GET /files/{id}` is unauthenticated on purpose — the kiosk browser fetches it
and cannot send a header. The random id is the capability, and ids are never
listed.

## Tests

```sh
pytest                  # no browser needed
ROOM_SMOKE=1 pytest -s  # drives a real kiosk browser
```
