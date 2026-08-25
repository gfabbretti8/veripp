"""Accepted findings.

A verifier pointed at existing code reports everything at once; failing the
build on all of it gets the check removed. The baseline is what makes the
Action usable on a real codebase, so the semantics have to be exact: known
findings must not fail, new ones must, and the file must survive the code
moving around underneath it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUGGY = "int mean(int a,int b){ return (a+b)/2; }\n"
CLEAN = "int clamp(int x){ if(x<0) return 0; if(x>100) return 100; return x; }\n"


def veripp(*args: str, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "veripp.cli", *args],
        capture_output=True, text=True, cwd=cwd or ROOT, timeout=1800,
    )


class TestKeyStability:
    """Identity is (file, function, property) -- never a line number."""

    def test_a_key_ignores_line_numbers(self) -> None:
        from veripp.baseline import Key

        assert Key("a.c", "f", "overflow") == Key("a.c", "f", "overflow")

    def test_paths_are_relative_so_a_checkout_can_move(self, tmp_path) -> None:
        """An absolute path bakes in one machine's directory layout and makes
        the baseline useless on a runner."""
        from veripp.baseline import key_for

        key = key_for(tmp_path / "src" / "a.c", "f", "overflow", root=tmp_path)
        assert key.file == "src/a.c"

    def test_a_path_outside_the_root_is_kept_as_is(self, tmp_path) -> None:
        from veripp.baseline import key_for

        key = key_for(Path("/elsewhere/a.c"), "f", "overflow", root=tmp_path)
        assert "a.c" in key.file


@pytest.mark.esbmc
class TestExitCodes:
    """What CI actually depends on."""

    @staticmethod
    def _project(tmp_path, body=BUGGY):
        (tmp_path / "m.c").write_text(body)
        return tmp_path

    def test_without_a_baseline_a_finding_fails(self, tmp_path) -> None:
        assert veripp("scan", "m.c", cwd=self._project(tmp_path)).returncode == 1

    def test_an_accepted_finding_does_not_fail(self, tmp_path) -> None:
        project = self._project(tmp_path)
        assert veripp("accept", "m.c", cwd=project).returncode == 0
        result = veripp("scan", "m.c", "--baseline", ".veripp-baseline", cwd=project)
        assert result.returncode == 0, result.stdout[-600:]
        assert "known finding" in result.stdout

    def test_a_new_finding_fails_and_is_named(self, tmp_path) -> None:
        project = self._project(tmp_path)
        veripp("accept", "m.c", cwd=project)
        (project / "m.c").write_text(BUGGY + "int fresh(int a,int b){ return a*b; }\n")
        result = veripp("scan", "m.c", "--baseline", ".veripp-baseline", cwd=project)
        assert result.returncode == 1
        assert "NEW finding" in result.stdout and "fresh" in result.stdout

    def test_moving_code_does_not_resurrect_a_finding(self, tmp_path) -> None:
        """The whole point of not keying on line numbers: adding anything
        above a function must not turn its accepted finding into a new one."""
        project = self._project(tmp_path)
        veripp("accept", "m.c", cwd=project)
        (project / "m.c").write_text("/* a new comment */\n\n\n" + BUGGY)
        result = veripp("scan", "m.c", "--baseline", ".veripp-baseline", cwd=project)
        assert result.returncode == 0, (
            "shifting a function down the file resurrected its accepted finding:\n"
            + result.stdout[-600:]
        )

    def test_a_clean_tree_with_a_baseline_still_passes(self, tmp_path) -> None:
        project = self._project(tmp_path, CLEAN)
        veripp("accept", "m.c", cwd=project)
        assert veripp("scan", "m.c", "--baseline", ".veripp-baseline",
                      cwd=project).returncode == 0


@pytest.mark.esbmc
class TestStaleEntries:
    def test_an_entry_that_no_longer_matches_is_reported(self, tmp_path) -> None:
        """An entry matching nothing still grants permission, and will go on
        granting it to whatever matches later."""
        (tmp_path / "m.c").write_text(BUGGY)
        veripp("accept", "m.c", cwd=tmp_path)
        (tmp_path / "m.c").write_text(CLEAN)
        result = veripp("scan", "m.c", "--baseline", ".veripp-baseline", cwd=tmp_path)
        assert "no longer occur" in result.stdout, result.stdout[-600:]


class TestFileHandling:
    """A baseline read wrongly suppresses real findings, so every failure to
    read one is loud."""

    def test_a_missing_baseline_is_an_error(self, tmp_path) -> None:
        (tmp_path / "m.c").write_text(CLEAN)
        result = veripp("scan", "m.c", "--baseline", "nope.json", cwd=tmp_path)
        assert result.returncode == 2
        assert "not found" in result.stderr

    def test_malformed_json_is_an_error(self, tmp_path) -> None:
        (tmp_path / "m.c").write_text(CLEAN)
        (tmp_path / "b.json").write_text("{not json")
        result = veripp("scan", "m.c", "--baseline", "b.json", cwd=tmp_path)
        assert result.returncode == 2
        assert "not valid JSON" in result.stderr

    def test_an_unknown_version_is_refused_not_guessed(self, tmp_path) -> None:
        (tmp_path / "m.c").write_text(CLEAN)
        (tmp_path / "b.json").write_text(json.dumps({"version": 99, "findings": []}))
        result = veripp("scan", "m.c", "--baseline", "b.json", cwd=tmp_path)
        assert result.returncode == 2
        assert "version" in result.stderr

    def test_a_malformed_entry_is_refused(self, tmp_path) -> None:
        (tmp_path / "m.c").write_text(CLEAN)
        (tmp_path / "b.json").write_text(
            json.dumps({"version": 1, "findings": [{"file": "a.c"}]})
        )
        result = veripp("scan", "m.c", "--baseline", "b.json", cwd=tmp_path)
        assert result.returncode == 2

    def test_the_file_is_sorted_so_it_diffs_cleanly(self, tmp_path) -> None:
        from veripp.baseline import Baseline, Entry, Key

        baseline = Baseline()
        for name in ("zeta", "alpha", "mid"):
            key = Key("a.c", name, "overflow")
            baseline.entries[key] = Entry(key=key)
        out = tmp_path / "b.json"
        baseline.save(out)
        names = [f["function"] for f in json.loads(out.read_text())["findings"]]
        assert names == sorted(names)

    def test_it_round_trips(self, tmp_path) -> None:
        from veripp.baseline import Baseline, Entry, Key

        key = Key("src/a.c", "f", "overflow")
        original = Baseline(entries={key: Entry(key=key, reason="legacy")})
        out = tmp_path / "b.json"
        original.save(out)
        assert Baseline.load(out).covers(key)
        assert Baseline.load(out).entries[key].reason == "legacy"
