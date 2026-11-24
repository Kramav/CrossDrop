import socket
import sys

LOOPBACK = ("127.0.0.1", "localhost")


def listeners(host: str, port: int) -> list[socket.socket]:
    """Bound sockets for `host` and, when that is not already loopback, for
    127.0.0.1 too.

    `tailscale serve` -- the https address streaming needs -- proxies to
    127.0.0.1 only, and the Pi binds its tailnet address. Loopback is reachable
    from this box alone, so it is not the 0.0.0.0 that PLAN.md §10 rules out.
    """
    socks = []
    for h in [host] + ([] if host in LOOPBACK else ["127.0.0.1"]):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((h, port))
        socks.append(s)
    return socks


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

        srv = load_config()["server"]
        host, port = srv["host"], int(srv["port"])
        uvicorn.Server(uvicorn.Config(app, host=host, port=port)).run(
            sockets=listeners(host, port))
        sys.exit(0)

    print("usage: python -m agent [selfcheck|serve]", file=sys.stderr)
    sys.exit(2)
