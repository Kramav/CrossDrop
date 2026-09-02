import sys

if sys.argv[1:] == ["selfcheck"]:
    from .selfcheck import selfcheck

    sys.exit(selfcheck())

# `serve` exists so that starting the agent does not require knowing the uvicorn
# incantation or a shell that can run `tailscale ip -4`. Host and port come from
# [server] in config.toml, so anything that can set ROOM_CONFIG can launch an
# agent — a test harness, a second instance, an orchestrator.
if sys.argv[1:] == ["serve"]:
    import uvicorn

    from .app import app, load_config

    srv = load_config()["server"]
    uvicorn.run(app, host=srv["host"], port=int(srv["port"]))
    sys.exit(0)

print("usage: python -m agent [selfcheck|serve]", file=sys.stderr)
sys.exit(2)
