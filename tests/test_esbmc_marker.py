"""When the checker-backed tests run.

Tests marked `esbmc` were skipped unless `esbmc` was on PATH. veripp looks in
four places -- $VERIPP_ESBMC, a checker from `veripp install-checker`, PATH,
and the one bundled with the wheel -- and the bundled one is the only checker
many installs have. On such a machine every test of the verification itself
was skipped, while veripp worked. The skip now asks veripp.
"""

import conftest


class _Item:
    def __init__(self, *marks):
        self.keywords = {mark: True for mark in marks}
        self.markers = []

    def add_marker(self, marker):
        self.markers.append(marker)


def _skipped(monkeypatch, tmp_path, bundled=None, named=None) -> bool:
    empty = tmp_path / "nothing-here"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("VERIPP_CHECKER_DIR", str(empty))
    if named is None:
        monkeypatch.delenv("VERIPP_ESBMC", raising=False)
    else:
        monkeypatch.setenv("VERIPP_ESBMC", named)
    monkeypatch.setattr("veripp.checker.bundled_esbmc", lambda: bundled)
    item = _Item("esbmc")
    conftest.pytest_collection_modifyitems(None, [item])
    return bool(item.markers)


def test_the_bundled_checker_is_enough(monkeypatch, tmp_path):
    assert not _skipped(monkeypatch, tmp_path, bundled="/site-packages/veripp_checker/bin/esbmc")


def test_a_named_checker_is_enough(monkeypatch, tmp_path):
    assert not _skipped(monkeypatch, tmp_path, named="/opt/esbmc/bin/esbmc")


def test_no_checker_anywhere_still_skips(monkeypatch, tmp_path):
    assert _skipped(monkeypatch, tmp_path)


def test_unmarked_tests_are_never_skipped(monkeypatch, tmp_path):
    empty = tmp_path / "nothing-here"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("VERIPP_ESBMC", raising=False)
    monkeypatch.setattr("veripp.checker.bundled_esbmc", lambda: None)
    item = _Item()
    conftest.pytest_collection_modifyitems(None, [item])
    assert not item.markers
