"""First-contact UX: what someone sees before they know how to use this.

These are the moments a CLI either earns another minute or gets closed. They
are cheap to get right and easy to regress, since nothing else in the suite
looks at them.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "veripp.cli", *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=300,
    )


class TestBareInvocation:
    def test_teaches_instead_of_scolding(self) -> None:
        """`veripp` alone is someone finding out what this is.

        argparse's default -- "error: the following arguments are required:
        command", exit 2 -- treats curiosity as a mistake.
        """
        result = run()
        assert result.returncode == 0, "asking what a tool does is not an error"
        assert "error" not in result.stderr.lower()
        for expected in ("doctor", "scan", "verify", "harness"):
            assert expected in result.stdout

    def test_states_the_exit_codes(self) -> None:
        """They are the contract for scripting it, and invisible otherwise."""
        out = run().stdout
        assert "Exit codes" in out
        for code in ("0", "1", "2", "3"):
            assert code in out

    def test_leads_with_what_makes_it_different(self) -> None:
        assert "not write the harness" in run().stdout


class TestVersion:
    def test_version_flag_exists(self) -> None:
        """Table stakes; it was missing entirely."""
        for flag in ("--version", "-V"):
            result = run(flag)
            assert result.returncode == 0, flag
            assert re.search(r"veripp \d+\.\d+", result.stdout), result.stdout

    def test_matches_pyproject(self) -> None:
        """__init__ once hard-coded 0.0.1 while pyproject said 0.1.0."""
        declared = re.search(
            r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M
        ).group(1)
        assert declared in run("--version").stdout


class TestMistakes:
    def test_a_typo_suggests_the_command_meant(self) -> None:
        result = run("scna", "foo.c")
        assert result.returncode == 2
        assert "veripp scan" in result.stderr, result.stderr

    def test_an_unrecognisable_command_still_lists_the_options(self) -> None:
        result = run("zzzzz")
        assert result.returncode == 2
        for command in ("verify", "harness", "scan", "doctor"):
            assert command in result.stderr

    def test_a_missing_argument_does_not_dump_the_whole_usage(self) -> None:
        """Thirty flags is the least useful thing to show at the moment
        someone forgot one."""
        result = run("verify")
        assert result.returncode == 2
        assert "required: source" in result.stderr
        assert "--help" in result.stderr
        assert result.stderr.count("\n") <= 4, result.stderr
