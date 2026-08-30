"""`roomctl <command>` — thin argparse shell over the functions in __init__.

Prints the agent's JSON reply verbatim: one output rule for every command, and
it pipes into jq. Errors go to stderr and exit 1, so scripts can branch on it.
"""

import argparse
import json
import sys

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

    def do_scroll():
        if a.top or a.bottom:
            return roomctl.scroll(a.target, a.screen, to="top" if a.top else "bottom")
        return roomctl.scroll(a.target, a.screen, dy=-a.dy if a.up else a.dy)

    try:
        result = {
            "status": lambda: roomctl.status(a.target),
            "screens": lambda: roomctl.screens(a.target),
            "reload": lambda: roomctl.reload(a.target, a.screen),
            "home": lambda: roomctl.home(a.target, a.screen),
            "navigate": lambda: roomctl.navigate(a.url, a.target, a.screen),
            "upload": lambda: roomctl.upload(a.path, a.target, a.screen),
            "scroll": do_scroll,
            "autoscroll": lambda: roomctl.autoscroll(a.action, a.target, a.screen, a.speed),
            "media": lambda: roomctl.media(a.action, a.target, a.screen, a.value),
        }[a.cmd]()
    except (RuntimeError, OSError) as e:
        print(f"roomctl: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0
