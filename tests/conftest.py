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
    config.addinivalue_line("markers", "esbmc: needs a checker veripp can find")


def pytest_collection_modifyitems(config, items):
    # Ask veripp, not PATH. It also finds $VERIPP_ESBMC, a checker from
    # `veripp install-checker` and the one bundled with the wheel -- the only
    # one many installs have -- and a skip for "esbmc not on PATH" then hid
    # every checker-backed test from someone whose veripp works fine.
    from veripp.esbmc import find_esbmc

    if find_esbmc():
        return
    skip = pytest.mark.skip(reason="no checker: veripp cannot find esbmc")
    for item in items:
        if "esbmc" in item.keywords:
            item.add_marker(skip)
