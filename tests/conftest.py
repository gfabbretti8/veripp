import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).resolve().parent / "golden"


@pytest.fixture(scope="session")
def repo() -> Path:
    return REPO


@pytest.fixture(scope="session")
def examples() -> Path:
    return REPO / "examples"


@pytest.fixture
def golden():
    """Read a pinned ESBMC transcript captured by tests/golden/capture.sh."""

    def _read(name: str) -> str:
        return (GOLDEN / f"{name}.txt").read_text(encoding="utf-8")

    return _read


def pytest_configure(config):
    config.addinivalue_line("markers", "esbmc: needs the esbmc binary on PATH")


def pytest_collection_modifyitems(config, items):
    if shutil.which("esbmc"):
        return
    skip = pytest.mark.skip(reason="esbmc not on PATH")
    for item in items:
        if "esbmc" in item.keywords:
            item.add_marker(skip)
