"""The README is the front door: every command in it is a promise.

Commands using illustrative paths (src/parser.cpp) cannot be run, but the ones
pointing at files in this repository can be -- along with the claims made about
what they do. A README that says "finds a real bug" next to a command that
verifies clean is worse than one that says nothing.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")

EXIT_VERIFIED, EXIT_COUNTEREXAMPLE, EXIT_USAGE = 0, 1, 2


def veripp(*args: str, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "veripp.cli", *args],
        capture_output=True, text=True, cwd=ROOT, timeout=timeout,
    )


@pytest.mark.esbmc
class TestQuickStartClaims:
    """The three commands under "Quick start", and what they are said to do."""

    def test_doctor_reports_ready(self) -> None:
        result = veripp("doctor")
        assert result.returncode == 0
        assert "ready" in result.stdout

    def test_ring_buffer_push_proves_a_postcondition(self) -> None:
        """README: "proves a postcondition"."""
        result = veripp("verify", "examples/ring_buffer.cpp", "--function", "push")
        assert result.returncode == EXIT_VERIFIED, result.stdout[-800:]

    def test_off_by_one_finds_a_real_bug(self) -> None:
        """README: "finds a real bug". If this ever verifies clean, either the
        example was fixed or the checker stopped working -- both worth knowing."""
        result = veripp("verify", "examples/off_by_one.cpp", "--function", "sum_array")
        assert result.returncode == EXIT_COUNTEREXAMPLE, result.stdout[-800:]

    def test_harness_can_be_inspected(self) -> None:
        result = veripp("harness", "examples/off_by_one.cpp", "--function", "sum_array")
        assert result.returncode == 0
        assert "VERIPP_NONDET" in result.stdout

    def test_the_class_example_runs(self) -> None:
        result = veripp(
            "verify", "examples/ring_buffer.cpp", "--class", "RingBuffer",
            "--max-calls", "6",
        )
        assert result.returncode != EXIT_USAGE, result.stderr[-800:]


class TestProviderExamples:
    """Every `--model provider:model` the README lists must at least be a
    provider veripp knows. Without credentials it should say so -- and must
    not say "unknown provider", which would mean the README is advertising
    something unsupported."""

    @pytest.mark.parametrize(
        "spec",
        [
            m for m in re.findall(r"--model (\S+)", README)
            if ":" in m and not m.startswith("provider")
        ],
    )
    def test_provider_is_recognised(self, spec: str) -> None:
        from veripp.llm import PROVIDERS

        provider = spec.split(":", 1)[0]
        assert provider in PROVIDERS, (
            f"README advertises --model {spec}, but '{provider}' is not a "
            f"known provider: {sorted(PROVIDERS)}"
        )


class TestCompletionSnippet:
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="shell completions are for POSIX shells; git-bash on Windows is not where they are used",
    )
    def test_the_documented_eval_line_works(self, tmp_path) -> None:
        """README: eval "$(veripp completion bash)"."""
        import shutil

        if not shutil.which("bash"):
            pytest.skip("bash")
        script = tmp_path / "c.bash"
        script.write_text(veripp("completion", "bash").stdout, encoding="utf-8")
        loaded = subprocess.run(
            ["bash", "-c", f'source "{script}" && complete -p veripp'],
            capture_output=True, text=True,
        )
        assert loaded.returncode == 0, loaded.stderr


@pytest.mark.esbmc
class TestDemoTimingClaim:
    def test_the_demo_is_roughly_as_quick_as_advertised(self, tmp_path) -> None:
        """README: "a few seconds, clones stb for you".

        Measured at 6s here including a cold clone of stb. The bound below is
        deliberately loose: it exists to catch the demo quietly becoming
        minutes, not to police seconds on someone else's hardware.
        """
        import shutil

        if not shutil.which("git"):
            pytest.skip("git")
        started = time.monotonic()
        result = subprocess.run(
            ["bash", str(ROOT / "demo/cve-2019-13223/run.sh"), str(tmp_path)],
            capture_output=True, text=True, timeout=1800,
        )
        elapsed = time.monotonic() - started
        if "fatal" in result.stderr and "clone" in result.stderr:
            pytest.skip("demo needs to clone stb")
        assert "Result: counterexample" in result.stdout
        assert "Result: verified" in result.stdout
        assert elapsed < 180, (
            f"the README says ~30 seconds; this took {elapsed:.0f}s"
        )


class TestReadmeWorkflows:
    """The README hands people a workflow to copy. It has to parse, and it has
    to keep the ordering that makes it work."""

    @staticmethod
    def _workflows():
        import yaml

        blocks = re.findall(r"```yaml\n(.*?)```", README, re.S)
        out = []
        for block in blocks:
            doc = yaml.safe_load(block)
            if isinstance(doc, dict) and "jobs" in doc:
                out.append(doc)
        return out

    def test_every_yaml_block_parses(self) -> None:
        yaml = pytest.importorskip("yaml")
        for block in re.findall(r"```yaml\n(.*?)```", README, re.S):
            yaml.safe_load(block)

    def test_the_ci_example_uploads_sarif_before_failing(self) -> None:
        """Without this ordering a finding fails the job before the SARIF is
        uploaded, and the annotations explaining the failure never appear."""
        pytest.importorskip("yaml")
        workflows = self._workflows()
        assert workflows, "no complete workflow in the README"
        steps = workflows[0]["jobs"]["veripp"]["steps"]

        verify = next(i for i, s in enumerate(steps) if "veripp@" in str(s.get("uses", "")))
        upload = next(i for i, s in enumerate(steps) if "upload-sarif" in str(s.get("uses", "")))
        assert upload > verify, "SARIF is uploaded before veripp runs"
        assert steps[verify].get("continue-on-error") is True, (
            "without continue-on-error the job dies before the upload"
        )
        assert any("exit 1" in str(s.get("run", "")) for s in steps[upload:]), (
            "nothing fails the job after the upload, so findings never go red"
        )

    def test_it_requests_the_permission_sarif_needs(self) -> None:
        pytest.importorskip("yaml")
        workflow = self._workflows()[0]
        assert workflow.get("permissions", {}).get("security-events") == "write"
