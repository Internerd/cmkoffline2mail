"""Ermoeglicht den Aufruf via ``python -m cmkoffline2mail``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
