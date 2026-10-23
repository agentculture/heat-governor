"""Entry point for ``python -m heat_governor``."""

from __future__ import annotations

import sys

from heat_governor.cli import main

if __name__ == "__main__":
    sys.exit(main())
