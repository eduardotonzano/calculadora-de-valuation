"""Shared CLI error handling for the valuation calculator scripts."""

from __future__ import annotations

import sqlite3
import sys
from contextlib import contextmanager


@contextmanager
def friendly_errors():
    """Catch the exceptions this codebase raises for bad input (unknown
    ticker/scenario, out-of-range explicit_years, missing data) and print
    a one-line message instead of a full traceback. Anything else (a real
    bug) still raises normally."""
    try:
        yield
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except sqlite3.OperationalError as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        sys.exit(1)
