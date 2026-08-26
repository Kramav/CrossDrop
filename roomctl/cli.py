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
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="is the display up, and what is it showing")
    sub.add_parser("reload", help="re-navigate to the current url")
    sub.add_parser("home", help="back to the configured home_url")
    sub.add_parser("navigate", help="point the display at a url").add_argument("url")
    sub.add_parser("upload", help="send a file and show it").add_argument("path")
    a = p.parse_args(argv)

    try:
        result = {
            "status": lambda: roomctl.status(a.target),
            "reload": lambda: roomctl.reload(a.target),
            "home": lambda: roomctl.home(a.target),
            "navigate": lambda: roomctl.navigate(a.url, a.target),
            "upload": lambda: roomctl.upload(a.path, a.target),
        }[a.cmd]()
    except (RuntimeError, OSError) as e:
        print(f"roomctl: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0
