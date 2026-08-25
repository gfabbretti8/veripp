"""The job summary is the first thing anyone sees on a run page.

It is also allowed to fail: the action discards this script's exit status,
because a formatting problem must never turn a real verification result into a
failed step. Both halves of that contract are tested here -- that it renders
what matters, and that nothing makes it blow up.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github/summary.py"


def render(status: str, report=None, tmp_path=None) -> subprocess.CompletedProcess:
    env = {"VERIPP_STATUS": status, "PATH": "/usr/bin:/bin"}
    if report is not None:
        path = tmp_path / "report.json"
        path.write_text(report if isinstance(report, str) else json.dumps(report))
        env["VERIPP_REPORT"] = str(path)
    return subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env, timeout=120
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
            capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=120,
        )
        assert result.returncode == 0
        assert result.stdout.strip()

    def test_unexpected_json_shapes(self, tmp_path) -> None:
        for junk in ([], "a string", {"candidates": None}, {"violated_property": 42}):
            result = render("1", junk, tmp_path)
            assert result.returncode == 0, junk
