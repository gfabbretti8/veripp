"""Locating the headers and scratch space veripp needs at runtime."""

from __future__ import annotations

import tempfile
from pathlib import Path


def contracts_include_dir() -> Path | None:
    """Directory to put on ESBMC's include path so `veripp/contracts.hpp` resolves.

    The header ships inside the package (see `package-data` in pyproject), so
    this works the same from a wheel, an editable install, `uv tool install`
    and `uvx`. The walk up the tree is a fallback for running straight out of
    a source tree that has not been installed at all.
    """
    packaged = Path(__file__).resolve().parent / "include"
    if (packaged / "veripp" / "contracts.hpp").is_file():
        return packaged
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "include" / "veripp" / "contracts.hpp"
        if candidate.is_file():
            return candidate.parent.parent
    return None


def scratch_dir(prefix: str = "veripp-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))
