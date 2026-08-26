import sys

if sys.argv[1:] == ["selfcheck"]:
    from .selfcheck import selfcheck

    sys.exit(selfcheck())

print("usage: python -m agent selfcheck", file=sys.stderr)
sys.exit(2)
