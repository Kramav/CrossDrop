import os
import socket
import sys

LOOPBACK = ("127.0.0.1", "localhost", "::1")
WILDCARD = ("0.0.0.0", "::", "")


def listeners(host: str, port: int) -> list[socket.socket]:
    """Bound sockets for `host` and, when that is a specific non-loopback
    address, for 127.0.0.1 too.

    `tailscale serve` -- the https address streaming needs -- proxies to
    127.0.0.1 only, and the Pi binds its tailnet address. Loopback is reachable
    from this box alone, so it is not the 0.0.0.0 that PLAN.md §10 rules out. A
    wildcard host already covers loopback, and binding both is EADDRINUSE.
    """
    hosts = [host] + ([] if host in LOOPBACK + WILDCARD else ["127.0.0.1"])
    socks = []
    for h in hosts:
        s = socket.socket(socket.AF_INET6 if ":" in h else socket.AF_INET,
                          socket.SOCK_STREAM)
        # What `uvicorn --host` did and this replaced. Without it every restart
        # fails to bind for ~60s: the kiosk polls us constantly, so there are
        # always connections we closed sitting in TIME_WAIT -- long enough for
        # update.sh to call a good release dead and roll it back. Not on
        # Windows, where the same flag lets a second process steal the port.
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((h, port))
        socks.append(s)
    return socks


def host_from_env() -> str | None:
    """CROSSDROP_HOST, or None when unset. Set but empty is an error: on the Pi
    that is `tailscale ip -4` printing nothing at boot, and falling back to
    127.0.0.1 would bring up an agent nobody can reach that never exits -- so
    systemd never retries it."""
    if "CROSSDROP_HOST" not in os.environ:
        return None
    host = os.environ["CROSSDROP_HOST"].strip()
    if not host:
        raise SystemExit("CROSSDROP_HOST is set but empty (tailscale not up yet?)"
                         " -- exiting so systemd retries")
    return host


if __name__ == "__main__":
    if sys.argv[1:] == ["selfcheck"]:
        from .selfcheck import selfcheck

        sys.exit(selfcheck())

    # `serve` exists so that starting the agent does not require knowing the
    # uvicorn incantation. Host and port come from [server] in config.toml, or
    # CROSSDROP_HOST (the Pi's unit passes `tailscale ip -4`), so anything that
    # can set an environment variable can launch an agent.
    if sys.argv[1:] == ["serve"]:
        import uvicorn

        from .app import app, load_config

        host_from_env()                     # refuse an empty one before binding
        srv = load_config()["server"]
        host, port = srv["host"], int(srv["port"])
        uvicorn.Server(uvicorn.Config(app, host=host, port=port)).run(
            sockets=listeners(host, port))
        sys.exit(0)

    print("usage: python -m agent [selfcheck|serve]", file=sys.stderr)
    sys.exit(2)
