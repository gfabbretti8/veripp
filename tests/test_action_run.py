"""The action's verdict step, run the way a runner runs it.

The step turns veripp's exit code and JSON report into pass or fail. It is
shell inside YAML, which nothing else in the suite executes, and it treated
exit 3 as a pass whatever caused it -- a vacuous proof included, though the
README promises "a vacuous proof can never pass CI". This runs the step's own
script under `bash -e -o pipefail`, as GitHub does for a composite step, with
a stand-in for `uv` that writes a chosen report and exits with a chosen code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the step is bash, run on Linux and macOS runners"
)

VACUOUS_VERIFY = {"outcome": "verified", "vacuous": True}
TIMED_OUT_VERIFY = {"outcome": "timeout", "vacuous": False}
VACUOUS_SCAN = {"source": "a.c", "inconclusive": [{"function": "f", "outcome": "vacuous"}]}
TIMED_OUT_SCAN = {"source": "a.c", "inconclusive": [{"function": "f", "outcome": "timeout"}]}


def _step_script() -> str:
    steps = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))["runs"]["steps"]
    return next(step for step in steps if step.get("name") == "Run veripp")["run"]


def _run(tmp_path, status: int, report: dict, fail_on: str = "counterexample"):
    """The step's exit code, and what it wrote to $GITHUB_OUTPUT."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "# Write the report wherever --json-out points, then exit as told.\n"
        'while [ $# -gt 0 ]; do\n'
        '  if [ "$1" = "--json-out" ]; then cp "$FAKE_REPORT" "$2"; fi\n'
        "  shift\n"
        "done\n"
        'exit "$FAKE_STATUS"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    canned = tmp_path / "canned.json"
    canned.write_text(json.dumps(report), encoding="utf-8")
    outputs = tmp_path / "outputs"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_REPORT": str(canned),
        "FAKE_STATUS": str(status),
        "VERIPP_SOURCE": "a.c",
        "VERIPP_FUNCTION": "f" if "vacuous" in report else "",
        "VERIPP_ARGS": "",
        "VERIPP_FAIL_ON": fail_on,
        "VERIPP_BASELINE": "",
        "VERIPP_SARIF": "",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(outputs),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "GITHUB_ACTION_PATH": str(ROOT),
    }
    done = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", _step_script()],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )
    written = dict(
        line.split("=", 1) for line in outputs.read_text(encoding="utf-8").splitlines()
        if "=" in line
    ) if outputs.exists() else {}
    return done.returncode, written, done.stdout


@pytest.mark.parametrize("report", [VACUOUS_VERIFY, VACUOUS_SCAN], ids=["verify", "scan"])
def test_a_vacuous_proof_fails_by_default(tmp_path, report):
    code, outputs, stdout = _run(tmp_path, 3, report)
    assert code != 0, "a vacuous proof passed the job"
    assert outputs["vacuous"] == "true"
    assert "::error::" in stdout and "vacuous" in stdout


@pytest.mark.parametrize("report", [TIMED_OUT_VERIFY, TIMED_OUT_SCAN], ids=["verify", "scan"])
def test_an_ordinary_inconclusive_still_only_warns_by_default(tmp_path, report):
    code, outputs, stdout = _run(tmp_path, 3, report)
    assert code == 0
    assert outputs["vacuous"] == "false"
    assert "::warning::" in stdout


def test_fail_on_never_still_reports_without_failing(tmp_path):
    code, outputs, _ = _run(tmp_path, 3, VACUOUS_VERIFY, fail_on="never")
    assert code == 0
    assert outputs["vacuous"] == "true"


@pytest.mark.parametrize("status,report,expected", [
    (3, TIMED_OUT_VERIFY, 3),
    (3, VACUOUS_VERIFY, 3),
    (1, {"outcome": "counterexample"}, 1),
    (0, {"outcome": "verified", "vacuous": False}, 0),
])
def test_fail_on_inconclusive_fails_short_of_a_proof(tmp_path, status, report, expected):
    code, _, _ = _run(tmp_path, status, report, fail_on="inconclusive")
    assert code == expected


def test_an_unknown_fail_on_is_an_error_not_a_default(tmp_path):
    """A typo such as `fail-on: inconclusve` must not quietly mean something."""
    code, _, stdout = _run(tmp_path, 3, TIMED_OUT_VERIFY, fail_on="inconclusve")
    assert code == 2
    assert "fail-on must be" in stdout
