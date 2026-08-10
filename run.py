#!/usr/bin/env python3
"""Launcher that works straight from a checkout, with no pip install.

    python run.py --camera 0

Handy on the Pi where the service runs from the source tree, and on Windows
during tuning. `pip install -e .` plus `python -m cycletime.main` also works.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from cycletime.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
