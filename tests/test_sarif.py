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

    # Worded as ESBMC 8.5 reports them, with the CWEs it gives each. The
    # heap failures all begin "dereference failure", and every one of them
    # was filed under the null-pointer rule; the rest fell to "other".
    @pytest.mark.parametrize("description,expected,cwe", [
        ("dereference failure: forgotten memory: dynamic_1_array", "leak", "CWE-401"),
        ("dereference failure: invalidated dynamic object", "use-after-free", "CWE-416"),
        ("dereference failure: invalidated dynamic object freed", "invalid-free", "CWE-415"),
        ("dereference failure: free() of non-dynamic memory", "invalid-free", "CWE-590"),
        ("Operand of free must have zero pointer offset", "invalid-free", "CWE-761"),
        ("use of uninitialized variable: x", "uninitialised", "CWE-457"),
        ("undefined behavior on shift operation shl", "shift", "CWE-1335"),
        ("NaN on ieee_div", "nan", "CWE-681"),
        ("dereference failure: NULL pointer", "pointer", "CWE-476"),
        ("arithmetic overflow on floating-point ieee_div", "overflow", "CWE-190"),
    ])
    def test_each_default_check_has_its_own_rule(self, description, expected, cwe) -> None:
        from veripp.sarif import RULES, rule_for

        assert rule_for(description) == expected
        assert cwe in RULES[expected][2]


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
        # Build the paths from the platform's own root: "/repo" is not an
        # absolute path on Windows, so relative_to never matches there and the
        # test fails on a path convention rather than on the code.
        root = Path.cwd() / "repo"
        finding = [{"file": str(root / "src" / "a.c"), "line": 1, "function": "f",
                    "property": "overflow", "cwes": []}]
        from veripp.sarif import build

        log = build(finding, root=root, version="0")
        uri = log["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        assert uri.replace("\\", "/") == "src/a.c"

    def test_the_message_carries_the_bound(self) -> None:
        """A consumer shows only this text. A reader must not take a bounded
        result for a total one."""
        log = self._log(bounds="bounded, unwind=8")
        assert "unwind=8" in log["runs"][0]["results"][0]["message"]["text"]

    def test_each_result_carries_its_own_bounds(self) -> None:
        """A tree scan has no single config to state, and a retry can widen
        a result past the one the scan started from."""
        from veripp.sarif import build

        findings = [
            {"file": "a.c", "line": 1, "function": "f", "property": "overflow",
             "cwes": [], "bounds": "bounded, unwind=512"},
            {"file": "b.c", "line": 1, "function": "g", "property": "overflow",
             "cwes": [], "bounds": "incremental BMC"},
        ]
        log = build(findings, root=Path("."), version="0", bounds="bounded, unwind=32")
        first, second = (r["message"]["text"] for r in log["runs"][0]["results"])
        assert "unwind=512" in first and "unwind=32" not in first
        assert "incremental BMC" in second

    def test_each_result_carries_the_cwes_esbmc_gave_it(self) -> None:
        """The rule names the class; ESBMC names the weakness, and a write
        (CWE-787) is not a read (CWE-125)."""
        from veripp.sarif import build

        log = build([{"file": "a.c", "line": 1, "function": "f",
                      "property": "dereference failure: invalidated dynamic object",
                      "cwes": ["CWE-416", "CWE-825"]}], root=Path("."), version="0")
        assert log["runs"][0]["results"][0]["properties"]["cwe"] == ["CWE-416", "CWE-825"]

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
        (tmp_path / "m.c").write_text(BUGGY, encoding="utf-8")
        out = tmp_path / "r.sarif"
        veripp("scan", "m.c", "--sarif", str(out), cwd=tmp_path)
        log = json.loads(out.read_text(encoding="utf-8"))
        assert log["runs"][0]["results"], "no results written"

    def test_a_tree_scan_states_its_bounds(self, tmp_path) -> None:
        """It stated none: _write_sarif had no config for a directory."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "m.c").write_text(BUGGY, encoding="utf-8")
        out = tmp_path / "r.sarif"
        veripp("scan", "src", "--sarif", str(out), "--no-cache", cwd=tmp_path)
        log = json.loads(out.read_text(encoding="utf-8"))
        (result,) = log["runs"][0]["results"]
        assert "unwind=32" in result["message"]["text"]

    def test_a_result_settled_on_retry_states_the_bound_it_needed(self, tmp_path) -> None:
        """The division is past a 200-iteration loop: the first pass runs out
        at unwind 32 * 4, and the retry reaches it at 32 * 16."""
        (tmp_path / "d.c").write_text(
            "int deep_div(int d) {\n"
            "    int s = 0;\n"
            "    for (int i = 0; i < 200; i++) s += 1;\n"
            "    return s / d;\n"
            "}\n", encoding="utf-8")
        out = tmp_path / "r.sarif"
        veripp("scan", "d.c", "--sarif", str(out), "--no-cache", "--no-llm", cwd=tmp_path)
        (result,) = json.loads(out.read_text(encoding="utf-8"))["runs"][0]["results"]
        assert "unwind=512" in result["message"]["text"], result["message"]["text"]

    def test_sarif_failure_does_not_lose_the_verification(self, tmp_path) -> None:
        """A reporting format must not cost someone a result they paid for."""
        (tmp_path / "m.c").write_text(BUGGY, encoding="utf-8")
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
