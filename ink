#!/usr/bin/env python3
"""
Ink CLI Wrapper

Purpose
This file is the executable entrypoint for Inkly.

It is intentionally minimal and contains no runtime logic.
All real functionality lives in `ink_core.py`.

Responsibilities
1. Prefer the repository runtime when this launcher is executed from a source checkout.
2. Otherwise detect the installed runtime layout under ~/.inkly.
3. Inject ~/.inkly/lib into sys.path for installed launchers.
4. Import and execute ink_core.main().

Why This Exists
We separate CLI execution from runtime logic so that:

- ink_core.py is fully importable (for testing and reuse)
- Unit tests can import runtime logic without installation
- The installed CLI can resolve modules correctly
- A source checkout cannot silently execute a stale installed runtime

Important Ordering Rule
sys.path must be configured BEFORE importing ink_core.
"""

import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SOURCE_PACKAGE_DIR = SCRIPT_DIR / "inkly"
DEFAULT_INKLY_HOME = Path.home() / ".inkly"
LIB_DIR = DEFAULT_INKLY_HOME / "lib"

# A bare ./ink otherwise uses the system Python even after setup installed the
# scraper and its dependencies into Inkly's venv. Respect an explicitly chosen
# developer venv, but use the installed interpreter for normal source launches.
VENV_PYTHON = DEFAULT_INKLY_HOME / "venv" / "bin" / "python"
if (
    SOURCE_PACKAGE_DIR.is_dir()
    and sys.prefix == sys.base_prefix
    and Path(sys.executable).absolute() != VENV_PYTHON.absolute()
    and VENV_PYTHON.is_file()
    and os.access(VENV_PYTHON, os.X_OK)
):
    os.execv(
        str(VENV_PYTHON),
        [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
    )

# The installer copies this launcher into Inkly's bin directory, where there is
# no sibling `inkly` package. In that installed layout, prepend ~/.inkly/lib.
# When the launcher is run directly from a repository checkout, the sibling
# package is authoritative and must not be shadowed by an older installed copy.
if not SOURCE_PACKAGE_DIR.is_dir() and LIB_DIR.exists():
    sys.path.insert(0, str(LIB_DIR))

from inkly.ink_core import main  # noqa: E402 - choose runtime before importing it


if __name__ == "__main__":
    sys.exit(main())
