"""Locating the headers and scratch space veripp needs at runtime."""

from __future__ import annotations

import tempfile
from pathlib import Path


def contracts_include_dir() -> Path | None:
    """Directory to put on ESBMC's include path so `veripp/contracts.hpp` resolves.

    Walks up from this module looking for the bundled header. That covers a
    source checkout and an editable install; packaging the header into a wheel
    is an M2 task (the header has to ship with the slicer anyway).
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "include" / "veripp" / "contracts.hpp"
        if candidate.is_file():
            return candidate.parent.parent
    return None


def scratch_dir(prefix: str = "veripp-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))
