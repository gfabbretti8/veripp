"""SARIF output.

Code scanning ingests SARIF and puts each finding on the pull request diff.
That is the difference between a result somebody has to go looking for in a
job log and one that appears beside the line causing it -- so the file has to
be valid, and it has to carry enough that a reader can judge the result
without veripp's own reporting around it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUGGY = "int mean(int a,int b){ return (a+b)/2; }\n"


def veripp(*args: str, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "veripp.cli", *args],
        capture_output=True, text=True, cwd=cwd or ROOT, timeout=1800,
    )


class TestRuleClassification:
    @pytest.mark.parametrize("description,expected", [
        ("arithmetic overflow on add", "overflow"),
        ("array bounds violated: array `t' lower bound", "bounds"),
        ("dereference failure: invalid pointer", "pointer"),
        ("division by zero", "division"),
        ("something nobody anticipated", "other"),
    ])
    def test_properties_map_to_rules(self, description, expected) -> None:
        from veripp.sarif import rule_for

        assert rule_for(description) == expected

    def test_every_rule_is_defined(self) -> None:
        from veripp.sarif import RULES, rule_for

        for description in ("overflow", "bounds", "null", "divide", "???"):
            assert rule_for(description) in RULES


class TestDocumentShape:
    @staticmethod
    def _log(**kwargs):
        from veripp.sarif import build

        findings = [{
            "file": "src/a.c", "line": 7, "column": 9, "function": "mean",
            "property": "arithmetic overflow on add", "cwes": ["CWE-190"],
        }]
        return build(findings, root=Path("/repo"), version="0.1.3", **kwargs)

    def test_declares_version_and_tool(self) -> None:
        log = self._log()
        assert log["version"] == "2.1.0"
        assert log["runs"][0]["tool"]["driver"]["name"] == "veripp"

    def test_a_finding_has_a_line(self) -> None:
        """Without one, code scanning cannot place an annotation on the diff."""
        region = self._log()["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]
        assert region["startLine"] == 7

    def test_paths_are_relative_to_the_checkout(self) -> None:
        """An absolute path from the runner matches no file in the diff."""
        finding = [{"file": "/repo/src/a.c", "line": 1, "function": "f",
                    "property": "overflow", "cwes": []}]
        from veripp.sarif import build

        log = build(finding, root=Path("/repo"), version="0")
        uri = log["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        assert uri == "src/a.c"

    def test_the_message_carries_the_bound(self) -> None:
        """A consumer shows only this text. A reader must not take a bounded
        result for a total one."""
        log = self._log(bounds="bounded, unwind=8")
        assert "unwind=8" in log["runs"][0]["results"][0]["message"]["text"]

    def test_the_message_says_a_finding_needs_triage(self) -> None:
        text = self._log()["runs"][0]["results"][0]["message"]["text"]
        assert "caller can reach it" in text

    def test_fingerprints_survive_code_moving(self) -> None:
        """Keyed like the baseline: file, function, property -- never a line."""
        prints = self._log()["runs"][0]["results"][0]["partialFingerprints"]
        value = next(iter(prints.values()))
        assert "7" not in value.split(":")[-1]
        assert "mean" in value

    def test_a_missing_line_still_produces_a_valid_region(self) -> None:
        from veripp.sarif import build

        log = build([{"file": "a.c", "function": "f", "property": "x", "cwes": []}],
                    root=Path("."), version="0")
        assert log["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]["startLine"] >= 1


class TestSuppression:
    def test_a_baselined_finding_is_suppressed_not_dropped(self) -> None:
        """Dropping it would make code scanning's count move when an entry is
        removed; suppressing shows it as accepted, which is what happened."""
        from veripp.sarif import build

        findings = [{"file": "a.c", "line": 1, "function": "f",
                     "property": "overflow", "cwes": []}]
        log = build(findings, root=Path("."), version="0",
                    suppressed={("a.c", "f", "overflow")})
        result = log["runs"][0]["results"][0]
        assert result["suppressions"][0]["kind"] == "external"
        assert "baseline" in result["suppressions"][0]["justification"].lower()

    def test_an_unaccepted_finding_is_not_suppressed(self) -> None:
        from veripp.sarif import build

        log = build([{"file": "a.c", "line": 1, "function": "other",
                      "property": "overflow", "cwes": []}],
                    root=Path("."), version="0",
                    suppressed={("a.c", "f", "overflow")})
        assert "suppressions" not in log["runs"][0]["results"][0]


@pytest.mark.esbmc
class TestEndToEnd:
    def test_scan_writes_valid_sarif(self, tmp_path) -> None:
        (tmp_path / "m.c").write_text(BUGGY)
        out = tmp_path / "r.sarif"
        veripp("scan", "m.c", "--sarif", str(out), cwd=tmp_path)
        log = json.loads(out.read_text())
        assert log["runs"][0]["results"], "no results written"

    def test_sarif_failure_does_not_lose_the_verification(self, tmp_path) -> None:
        """A reporting format must not cost someone a result they paid for."""
        (tmp_path / "m.c").write_text(BUGGY)
        result = veripp("scan", "m.c", "--sarif", "/nonexistent/dir/r.sarif",
                        cwd=tmp_path)
        assert result.returncode == 1, "the counterexample verdict was lost"
        assert "Scanned" in result.stdout


class TestSchemaConformance:
    """GitHub rejects invalid SARIF outright, so validate against the real
    schema rather than trusting the shape looks right."""

    def test_validates_against_sarif_2_1_0(self, tmp_path) -> None:
        import urllib.error
        import urllib.request

        jsonschema = pytest.importorskip("jsonschema")
        from veripp.sarif import build

        try:
            raw = urllib.request.urlopen(
                "https://json.schemastore.org/sarif-2.1.0.json", timeout=60
            ).read()
        except (urllib.error.URLError, OSError) as exc:
            pytest.skip(f"schema unreachable: {exc}")

        findings = [
            {"file": "src/a.c", "line": 7, "column": 9, "function": "mean",
             "property": "arithmetic overflow on add", "cwes": ["CWE-190"]},
            {"file": "src/b.c", "line": 3, "function": "pick",
             "property": "array bounds violated", "cwes": ["CWE-125"]},
        ]
        for suppressed in (set(), {("src/a.c", "mean", "arithmetic overflow on add")}):
            log = build(findings, root=Path("."), version="0.1.3",
                        bounds="bounded, unwind=8", suppressed=suppressed)
            jsonschema.validate(log, json.loads(raw))
