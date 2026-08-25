"""Run what the skill tells an agent to run.

A skill is a set of instructions an agent follows literally. If a documented
invocation is rejected as a usage error, the agent burns a turn, retries
something it invented, and learns to distrust the tool -- which is worse than
having no skill at all. So every flag combination the skill recommends is
executed here against real fixtures.

Verdicts are not asserted: 0 (verified), 1 (counterexample) and 3
(inconclusive) are all legitimate answers about a given file. What must never
happen is exit 2 -- veripp did not understand the command -- or a traceback.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills/veripp/SKILL.md"
EXIT_USAGE = 2

pytestmark = pytest.mark.esbmc


def veripp(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "veripp.cli", *args],
        capture_output=True, text=True, cwd=cwd or ROOT, timeout=900,
    )


def check(result: subprocess.CompletedProcess, what: str) -> None:
    combined = result.stdout + result.stderr
    assert "Traceback" in combined is False or "Traceback" not in combined, (
        f"{what} crashed:\n{combined[-1500:]}"
    )
    assert result.returncode != EXIT_USAGE, (
        f"the skill recommends `{what}`, and veripp rejected it as a usage "
        f"error:\n{combined[-1500:]}"
    )


class TestDocumentedInvocations:
    """Each of these mirrors a command block in SKILL.md, with the
    illustrative paths swapped for fixtures that exist."""

    def test_doctor(self) -> None:
        check(veripp("doctor"), "veripp doctor")

    def test_scan_a_file(self) -> None:
        check(veripp("scan", "examples/ring_buffer.cpp"), "veripp scan FILE")

    def test_verify_a_function(self) -> None:
        check(
            veripp("verify", "examples/off_by_one.cpp", "--function", "sum_array"),
            "veripp verify FILE --function F",
        )

    def test_scan_with_jobs_and_json(self) -> None:
        result = veripp("scan", "examples/ring_buffer.cpp", "--jobs", "4", "--json")
        check(result, "veripp scan FILE --jobs 4 --json")
        json.loads(result.stdout)  # must be parseable, since the skill says so

    def test_verify_with_an_assumption(self) -> None:
        check(
            veripp("verify", "examples/off_by_one.cpp", "--function", "sum_array",
                   "--assume", "n > 0"),
            "veripp verify FILE --function F --assume EXPR",
        )

    def test_class_with_max_calls_and_assert(self) -> None:
        check(
            veripp("verify", "examples/ring_buffer.cpp", "--class", "RingBuffer",
                   "--max-calls", "4", "--assert", "true"),
            "veripp verify FILE --class C --max-calls N --assert EXPR",
        )

    def test_harness_prints_without_verifying(self) -> None:
        result = veripp("harness", "examples/off_by_one.cpp", "--function", "sum_array")
        check(result, "veripp harness FILE --function F")
        assert "VERIPP_NONDET" in result.stdout

    def test_json_out_alongside_readable_output(self, tmp_path) -> None:
        report = tmp_path / "report.json"
        result = veripp("scan", "examples/ring_buffer.cpp", "--json-out", str(report))
        check(result, "veripp scan FILE --json-out PATH")
        assert "Scanned" in result.stdout, "readable output was replaced, not kept"
        assert json.loads(report.read_text())["candidates"]

    def test_compile_commands(self, tmp_path) -> None:
        """The skill shows --compile-commands; make sure the flag works with a
        database, including one written for a different absolute root."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.c").write_text("int scale(int x){ return x * 2; }\n")
        db = tmp_path / "build"
        db.mkdir()
        (db / "compile_commands.json").write_text(json.dumps([{
            "directory": str(db), "file": str(src / "a.c"),
            "command": f"cc -c {src / 'a.c'}",
        }]))
        check(
            veripp("verify", "src/a.c", "--function", "scale",
                   "--compile-commands", "build/compile_commands.json", cwd=tmp_path),
            "veripp verify FILE --compile-commands DB",
        )


class TestSkillStaysTrue:
    def test_every_documented_flag_is_exercised_above(self) -> None:
        """If SKILL.md gains an invocation, this file should gain a test.

        Guards the drift where a skill accumulates examples nobody ever runs,
        which is how documented commands quietly stop working.
        """
        import inspect

        blocks = re.findall(r"```bash\n(.*?)```", SKILL.read_text(), re.S)
        documented = {
            flag
            for block in blocks
            for line in block.splitlines()
            if line.strip().startswith("veripp ")
            for flag in re.findall(r"(?<![\w-])--[a-z][a-z0-9-]+", line)
        }
        exercised = set(
            re.findall(r'"(--[a-z][a-z0-9-]+)"',
                       inspect.getsource(TestDocumentedInvocations))
        )
        missing = documented - exercised
        assert not missing, (
            f"SKILL.md documents {sorted(missing)} but nothing here runs them"
        )
