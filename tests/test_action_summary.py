"""The job summary is the first thing anyone sees on a run page.

It is also allowed to fail: the action discards this script's exit status,
because a formatting problem must never turn a real verification result into a
failed step. Both halves of that contract are tested here -- that it renders
what matters, and that nothing makes it blow up.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github/summary.py"


def render(status: str, report=None, tmp_path=None) -> subprocess.CompletedProcess:
    # Inherit the real environment and override only what this test controls.
    # A hardcoded POSIX PATH leaves Python unable to start on Windows, where
    # the interpreter needs its own directories to load at all -- the process
    # then produces no output and every assertion fails against None.
    env = {**os.environ, "VERIPP_STATUS": status}
    env.pop("VERIPP_REPORT", None)
    if report is not None:
        path = tmp_path / "report.json"
        path.write_text(report if isinstance(report, str) else json.dumps(report), encoding="utf-8")
        env["VERIPP_REPORT"] = str(path)
    return subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, encoding="utf-8", env=env, timeout=120
    )


class TestVerdict:
    @pytest.mark.parametrize(
        "status,expected",
        [("0", "Verified"), ("1", "Counterexample"), ("2", "Usage error"), ("3", "Inconclusive")],
    )
    def test_each_exit_code_names_its_verdict(self, status, expected) -> None:
        assert expected in render(status).stdout

    def test_inconclusive_is_not_sold_as_a_pass(self) -> None:
        """The whole point of distinguishing exit 3 from exit 0."""
        out = render("3").stdout
        assert "not** a pass" in out or "not a pass" in out

    def test_an_unknown_status_is_treated_as_inconclusive(self) -> None:
        """Never round an unrecognised state up to success."""
        assert "Inconclusive" in render("99").stdout


class TestScanReport:
    REPORT = {
        "source": "src/parser.c",
        "candidates": 40,
        "proved": ["a", "b", "c"],
        "counterexamples": [{"function": "boom", "reason": "array bounds violated"}],
        "inconclusive": ["d"],
        "artifacts": [],
        "not_harnessable": {"opaque type": 2},
    }

    def test_counts_lists_rather_than_printing_them(self, tmp_path) -> None:
        """These arrive as lists, not integers -- the first version of this
        renderer silently dropped every row by testing isinstance(int)."""
        out = render("1", self.REPORT, tmp_path).stdout
        assert "| ✅ Proved | 3 |" in out
        assert "| ❌ Counterexamples | 1 |" in out
        assert "| — Not harnessable | 1 |" in out

    def test_names_the_findings_that_need_triage(self, tmp_path) -> None:
        out = render("1", self.REPORT, tmp_path).stdout
        assert "boom" in out and "array bounds violated" in out

    def test_long_lists_are_truncated(self, tmp_path) -> None:
        report = dict(self.REPORT, counterexamples=[{"function": f"f{i}"} for i in range(50)])
        out = render("1", report, tmp_path).stdout
        assert "and 30 more" in out


class TestVerifyReport:
    def test_renders_a_structured_violation(self, tmp_path) -> None:
        """violated_property is a dict; printing it raw dumped Python repr."""
        report = {
            "function": "sum_array",
            "violated_property": {
                "loc": {"file": "src/x.c", "line": 7, "column": 9, "function": "sum_array"},
                "description": "arithmetic overflow on add",
                "expression": '!overflow("+", s, a)',
                "cwes": ["CWE-190"],
            },
        }
        out = render("1", report, tmp_path).stdout
        assert "arithmetic overflow on add" in out
        assert "src/x.c:7:9" in out
        assert "CWE-190" in out
        assert "'loc':" not in out, "raw dict leaked into the summary"

    def test_a_bounded_proof_says_so(self, tmp_path) -> None:
        out = render("0", {"bounded": True, "function": "f"}, tmp_path).stdout
        assert "bounded" in out.lower()

    def test_a_vacuous_result_is_called_out(self, tmp_path) -> None:
        out = render("0", {"vacuous": True, "function": "f"}, tmp_path).stdout
        assert "acuous" in out and "not a proof" in out

    def test_an_unsound_checker_is_surfaced(self, tmp_path) -> None:
        out = render("0", {"unsound_probes": ["member-array bounds"]}, tmp_path).stdout
        assert "soundness probe" in out


class TestNeverBreaksTheStep:
    """The action swallows this script's status; make sure there is nothing
    to swallow."""

    def test_missing_report_file(self) -> None:
        result = render("1")
        assert result.returncode == 0
        assert "Counterexample" in result.stdout

    def test_malformed_json(self, tmp_path) -> None:
        result = render("3", "not json{", tmp_path)
        assert result.returncode == 0
        assert "Inconclusive" in result.stdout

    def test_no_environment_at_all(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True, text=True, encoding="utf-8",
            env={k: v for k, v in os.environ.items() if k != "VERIPP_REPORT"},
            timeout=120,
        )
        assert result.returncode == 0
        assert result.stdout.strip()

    def test_unexpected_json_shapes(self, tmp_path) -> None:
        for junk in ([], "a string", {"candidates": None}, {"violated_property": 42}):
            result = render("1", junk, tmp_path)
            assert result.returncode == 0, junk


class TestTreeScanReport:
    """A directory scan aggregates into counts; a single-file scan reports
    lists of names. Both reach this renderer, and it must count either."""

    REPORT = {
        "root": "src",
        "files": 3,
        "candidates": 40,
        "proved": 22,
        "counterexamples": [
            {"file": "src/parse.c", "function": "boom", "property": "array bounds violated"}
        ],
        "inconclusive": 17,
        "artifacts": 0,
    }

    def test_integer_counts_are_not_reported_as_zero(self, tmp_path) -> None:
        """size() counted only lists, so every proof in a tree scan rendered
        as 0 -- a silent, plausible-looking wrong answer."""
        out = render("1", self.REPORT, tmp_path).stdout
        assert "| ✅ Proved | 22 |" in out, out
        assert "| ⏱️ Inconclusive | 17 |" in out

    def test_names_the_directory_and_the_file_count(self, tmp_path) -> None:
        """A tree report has `root`, not `source`; reading only `source` fell
        back to the literal word "file"."""
        out = render("1", self.REPORT, tmp_path).stdout
        assert "**src**" in out and "across 3 files" in out
        assert "**file**" not in out

    def test_findings_say_which_file(self, tmp_path) -> None:
        out = render("1", self.REPORT, tmp_path).stdout
        assert "src/parse.c" in out and "boom" in out
        assert "array bounds violated" in out

    def test_the_single_file_shape_still_renders(self, tmp_path) -> None:
        single = {
            "source": "a.c", "candidates": 3,
            "proved": ["f", "g", "h"], "counterexamples": [],
            "inconclusive": [], "artifacts": [], "not_harnessable": {},
        }
        out = render("0", single, tmp_path).stdout
        assert "| ✅ Proved | 3 |" in out
        assert "**a.c**" in out and "across" not in out

    def test_a_boolean_is_not_counted_as_one(self, tmp_path) -> None:
        """bool is a subclass of int; True must not read as a count of 1."""
        out = render("0", dict(self.REPORT, artifacts=True), tmp_path).stdout
        assert "| 🔧 Harness artifacts | 0 |" in out


class TestOutputEncoding:
    """The summary writes ✅ and ❌. Python encodes stdout with the platform's
    preferred encoding, which on Windows is a codepage that has neither, so
    printing raised UnicodeEncodeError and the summary vanished entirely --
    the step showed nothing at all. Found on windows-latest.

    PYTHONIOENCODING reproduces it without a Windows machine.
    """

    def test_renders_under_a_codepage_without_the_symbols(self, tmp_path) -> None:
        import os
        import subprocess
        import sys as _sys

        env = {**os.environ, "VERIPP_STATUS": "0", "PYTHONIOENCODING": "cp437"}
        env.pop("VERIPP_REPORT", None)
        result = subprocess.run(
            [_sys.executable, str(SCRIPT)], capture_output=True, text=True,
            env=env, timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert "Verified" in result.stdout, result.stderr

    def test_an_unencodable_symbol_would_otherwise_fail(self) -> None:
        """Confirms the simulation is real rather than vacuous."""
        import subprocess
        import sys as _sys

        result = subprocess.run(
            [_sys.executable, "-c", "print('\\u2705')"],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONIOENCODING": "cp437"}, timeout=120,
        )
        assert result.returncode != 0
        assert "UnicodeEncodeError" in result.stderr
