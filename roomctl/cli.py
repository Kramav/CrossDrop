"""`roomctl <command>` — thin argparse shell over the functions in __init__.

Prints the agent's JSON reply verbatim: one output rule for every command, and
it pipes into jq. Errors go to stderr and exit 1, so scripts can branch on it.
"""

import argparse
import base64
import json
import sys
from pathlib import Path

import roomctl


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="roomctl", description="Drive a room display.")
    p.add_argument("-t", "--target", help="target name from targets.toml (default: its `default`)")
    # A target is a Pi; a screen is one of its monitors. "all" hits every screen.
    p.add_argument("-s", "--screen", help="screen name, or 'all' (default: the first)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="is the display up, and what is it showing")
    sub.add_parser("screens", help="list this display's screens")
    sub.add_parser("reload", help="re-navigate to the current url")
    sub.add_parser("home", help="back to the configured home_url")
    sub.add_parser("navigate", help="point the display at a url").add_argument("url")
    sub.add_parser("upload", help="send a file and show it").add_argument("path")

    # Extensions are per-display, not per-screen: -s does not apply.
    ext = sub.add_parser("extension", help="list, install or remove kiosk extensions")
    ext.add_argument("action", nargs="?", default="list",
                     choices=["list", "install", "remove"])
    ext.add_argument("what", nargs="*",
                     help="install: Web Store ids. remove: the id shown by list.")

    # Kept in step with agent/browser.py WINDOW_STATES by hand, same as media below.
    win = sub.add_parser("window", help="set the kiosk window aside, or put it back")
    win.add_argument("state", choices=["normal", "minimized", "fullscreen"])

    shot = sub.add_parser("shot", help="what the screen is actually showing")
    shot.add_argument("-o", "--out", help="write the image here (default: "
                                          "print the metadata only)")
    shot.add_argument("--region", help="x,y,width,height in CSS pixels, "
                                       "clamped to the viewport")
    shot.add_argument("--format", default="png", choices=["png", "jpeg", "webp"])
    shot.add_argument("--quality", type=int, default=80, help="jpeg/webp only")

    sub.add_parser("inspect", help="what the page says about itself")

    # One verb per subcommand rather than a JSON action list on the command
    # line: the list is what the *library* is for, and quoting JSON through two
    # shells is how you end up typing a password into the wrong field.
    click = sub.add_parser("click", help="click a selector, or an x y")
    click.add_argument("where", nargs="+", help="a CSS selector, or: X Y")
    click.add_argument("--double", action="store_true")
    click.add_argument("--right", action="store_true")

    typ = sub.add_parser("type", help="type text into whatever has focus")
    typ.add_argument("text")

    press = sub.add_parser("key", help="press a key, e.g. Enter or ctrl+a")
    press.add_argument("combo", help="Key, or mod+mod+Key")

    scroll = sub.add_parser("scroll", help="scroll the page")
    where = scroll.add_mutually_exclusive_group()
    where.add_argument("--down", action="store_true", help="down a screenful (default)")
    where.add_argument("--up", action="store_true", help="up a screenful")
    where.add_argument("--top", action="store_true")
    where.add_argument("--bottom", action="store_true")
    scroll.add_argument("--dy", type=int, default=600, help="pixels, if not --top/--bottom")

    auto = sub.add_parser("autoscroll", help="scroll slowly and continuously")
    auto.add_argument("action", choices=["start", "stop"])
    auto.add_argument("--speed", type=int, default=40, help="pixels per tick")

    # Kept in step with agent/browser.py MEDIA_ACTIONS by hand: roomctl talks to a
    # remote Pi and must not import the agent to run.
    med = sub.add_parser("media", help="control the video or audio on the page")
    med.add_argument("action", nargs="?", default="state",
                     choices=["state", "play", "pause", "toggle",
                              "mute", "unmute", "seek", "volume"])
    med.add_argument("value", nargs="?", type=int, default=0,
                     help="seek: seconds, may be negative. volume: 0-100.")

    a = p.parse_args(argv)

    def do_extension(c):
        if a.action == "install":
            if not a.what:
                raise RuntimeError("extension install needs at least one id")
            return c.extensions(install=a.what)
        if a.action == "remove":
            if len(a.what) != 1:
                raise RuntimeError("extension remove takes exactly one id")
            return c.extensions(remove=a.what[0])
        return c.extensions()

    def do_shot(c):
        region = None
        if a.region:
            try:
                x, y, w, h = (int(v) for v in a.region.split(","))
            except ValueError:
                raise RuntimeError(f"--region must be x,y,width,height, got {a.region!r}")
            region = {"x": x, "y": y, "width": w, "height": h}
        r = c.screenshot(a.screen, region, a.format, a.quality)
        # The image never goes to stdout. Every other command prints the agent's
        # reply verbatim so it pipes into jq, and a megabyte of base64 would
        # make that useless -- and dumping raw bytes into a terminal is worse.
        # What is left is exactly the part worth reading: size, url, title.
        image = base64.b64decode(r.pop("image"))
        if a.out:
            Path(a.out).write_bytes(image)
            r["written"] = a.out
        r["bytes"] = len(image)
        return r

    def do_click(c):
        do = "double" if a.double else "right" if a.right else "click"
        if len(a.where) == 2 and all(w.lstrip("-").isdigit() for w in a.where):
            act = {"do": do, "x": int(a.where[0]), "y": int(a.where[1])}
        elif len(a.where) == 1:
            act = {"do": do, "selector": a.where[0]}
        else:
            raise RuntimeError("click takes a selector, or two numbers: X Y")
        return c.input([act], a.screen)

    def do_key(c):
        *mods, key = a.combo.split("+")
        return c.input([{"do": "key", "key": key, "modifiers": mods}], a.screen)

    def do_scroll(c):
        if a.top or a.bottom:
            return c.scroll(a.screen, to="top" if a.top else "bottom")
        return c.scroll(a.screen, dy=-a.dy if a.up else a.dy)

    # Straight onto roomctl.Client — there is no by-name wrapper layer any more,
    # so a new endpoint is a Client method and a line here, not three places.
    try:
        with roomctl.client(a.target) as c:
            result = {
                "status": lambda: c.status(),
                "screens": lambda: c.screens(),
                "reload": lambda: c.reload(a.screen),
                "home": lambda: c.home(a.screen),
                "navigate": lambda: c.navigate(a.url, a.screen),
                "upload": lambda: c.upload(a.path, a.screen),
                "extension": lambda: do_extension(c),
                "window": lambda: c.window(a.state, a.screen),
                "shot": lambda: do_shot(c),
                "inspect": lambda: c.inspect(a.screen),
                "click": lambda: do_click(c),
                "type": lambda: c.input([{"do": "type", "text": a.text}], a.screen),
                "key": lambda: do_key(c),
                "scroll": lambda: do_scroll(c),
                "autoscroll": lambda: c.autoscroll(a.action, a.screen, a.speed),
                "media": lambda: c.media(a.action, a.screen, a.value),
            }[a.cmd]()
    except (RuntimeError, OSError) as e:
        print(f"roomctl: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0
