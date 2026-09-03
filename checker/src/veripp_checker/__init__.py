"""A bundled ESBMC, so `pip install veripp[checker]` is the whole install.

This package carries nothing of veripp's own logic. It exists because the
checker is a native binary that pip cannot otherwise deliver, and because the
build most people reach for by hand is the one that silently misses
out-of-bounds writes to a member array (esbmc#6508).

What is bundled is deliberately the *slim* build -- Z3 only, no Boolector,
Bitwuzla, MathSAT or Yices. That is both smaller and cleaner to redistribute:
ESBMC's own COPYING warns that several of the other solvers are academic or
non-commercial. The licence texts ship inside this wheel.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["esbmc_path", "version"]

version = "0.1.0"


def esbmc_path() -> str | None:
    """The bundled checker, or None if this wheel carries none for the platform.

    Returns a path only when the file is actually executable: a wheel whose
    permission bits were lost in transit should look absent rather than
    produce a confusing "permission denied" from deep inside a verification.
    """
    name = "esbmc.exe" if sys.platform == "win32" else "esbmc"
    candidate = Path(__file__).resolve().parent / "bin" / name
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None
