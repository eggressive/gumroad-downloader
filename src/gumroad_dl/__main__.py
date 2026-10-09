"""Allow ``python -m gumroad_dl``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
