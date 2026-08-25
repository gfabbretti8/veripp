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


class TestColour:
    """Colour on the verdict line, and nowhere it can leak.

    Every rule here is one other tools already follow, so nobody has to learn
    ours -- and every one of them is a way colour breaks someone's pipeline
    when it is missed.
    """

    def test_plain_when_piped(self) -> None:
        """capture_output means no TTY, which is also every script and CI log."""
        out = run("verify", "examples/off_by_one.cpp", "--function", "sum_array").stdout
        assert "Result: counterexample" in out
        assert "\033[" not in out, "ANSI escapes leaked into a pipe"

    def test_no_color_beats_force_color(self, monkeypatch) -> None:
        from veripp import term

        monkeypatch.setenv("FORCE_COLOR", "1")
        monkeypatch.setenv("NO_COLOR", "1")
        assert term.colour_enabled(_FakeTTY()) is False

    def test_force_color_overrides_a_missing_tty(self, monkeypatch) -> None:
        from veripp import term

        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        assert term.colour_enabled(_NotATTY()) is True

    def test_dumb_terminals_get_nothing(self, monkeypatch) -> None:
        from veripp import term

        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        monkeypatch.setenv("TERM", "dumb")
        assert term.colour_enabled(_FakeTTY()) is False

    def test_style_is_a_no_op_when_colour_is_off(self, monkeypatch) -> None:
        from veripp import term

        monkeypatch.setenv("NO_COLOR", "1")
        assert term.style("verified", "green", "bold") == "verified"

    def test_a_proof_and_a_refutation_look_different(self, monkeypatch) -> None:
        from veripp import term

        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        assert term.verdict("verified") != term.verdict("counterexample")

    def test_inconclusive_is_not_dressed_as_a_pass(self, monkeypatch) -> None:
        """The result people most often misread as success."""
        from veripp import term

        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        for weak in ("unwind_limit", "timeout", "unknown", "parse_error"):
            assert "\033[32m" not in term.verdict(weak), f"{weak} rendered as green"


class _FakeTTY:
    def isatty(self) -> bool:
        return True


class _NotATTY:
    def isatty(self) -> bool:
        return False


class TestJsonOut:
    """--json replaces stdout; --json-out adds a file. CI needs both, and
    should not have to verify twice to get them."""

    def test_writes_the_report_and_keeps_readable_output(self, tmp_path) -> None:
        out = tmp_path / "r.json"
        result = run(
            "verify", "examples/off_by_one.cpp", "--function", "sum_array",
            "--json-out", str(out),
        )
        assert "Result: counterexample" in result.stdout
        import json

        assert json.loads(out.read_text())["outcome"]

    def test_works_for_scan_too(self, tmp_path) -> None:
        out = tmp_path / "s.json"
        run("scan", "examples/ring_buffer.cpp", "--json-out", str(out))
        import json

        assert json.loads(out.read_text())["candidates"]

    def test_the_action_verifies_only_once(self) -> None:
        """Two runs per job doubled the cost of every verification."""
        action = (ROOT / "action.yml").read_text()
        block = action[action.index("Run veripp"):]
        assert block.count("veripp verify") == 1, "veripp verify invoked more than once"
        assert block.count("veripp scan") == 1, "veripp scan invoked more than once"


class TestCompletions:
    """Completions generated from the parser, so they cannot drift.

    Hand-written ones rot silently: a flag is added, the script is not
    updated, and the shell suggests options that no longer exist.
    """

    @pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
    def test_generates_without_error(self, shell) -> None:
        result = run("completion", shell)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip()

    def test_bash_script_is_valid_and_registers(self, tmp_path) -> None:
        import shutil
        import subprocess as sp

        if not shutil.which("bash"):
            pytest.skip("bash")
        path = tmp_path / "c.bash"
        path.write_text(run("completion", "bash").stdout)
        assert sp.run(["bash", "-n", str(path)], capture_output=True).returncode == 0
        loaded = sp.run(
            ["bash", "-c", f"source {path} && complete -p veripp"],
            capture_output=True, text=True,
        )
        assert "_veripp" in loaded.stdout

    def test_zsh_script_is_valid(self, tmp_path) -> None:
        import shutil
        import subprocess as sp

        if not shutil.which("zsh"):
            pytest.skip("zsh")
        path = tmp_path / "c.zsh"
        path.write_text(run("completion", "zsh").stdout)
        result = sp.run(["zsh", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
    def test_completions_track_the_real_flags(self, shell) -> None:
        """The point of generating them: a new flag appears automatically.

        fish spells long options `-l json-out`, without the leading dashes,
        so match the flag name rather than its bash/zsh spelling.
        """
        out = run("completion", shell).stdout
        for flag in ("json-out", "function", "assume", "unwind"):
            assert flag in out, f"{shell} completion is missing --{flag}"

    def test_lists_every_subcommand(self) -> None:
        out = run("completion", "bash").stdout
        for command in ("verify", "harness", "scan", "doctor"):
            assert command in out

    def test_an_unknown_shell_is_rejected(self) -> None:
        assert run("completion", "powershell").returncode == 2


class TestMistypedTarget:
    """Function and class names are the two things people mistype most, and
    the file already contains the correct spelling."""

    def test_a_mistyped_function_suggests_the_real_one(self) -> None:
        result = run("verify", "examples/off_by_one.cpp", "--function", "sumArray")
        assert result.returncode == 2
        assert "sum_array" in result.stderr

    def test_a_mistyped_class_suggests_a_class_not_a_function(self) -> None:
        """It used to answer "class Ringbuffer not found -- this file defines
        push, pop, size", which names the wrong kind of thing and hides a
        one-letter fix."""
        result = run("verify", "examples/ring_buffer.cpp", "--class", "Ringbuffer")
        assert result.returncode == 2
        assert "RingBuffer" in result.stderr
        for method in ("push", "pop", "size"):
            assert f"did you mean: {method}" not in result.stderr

    def test_an_unrecognisable_class_lists_the_classes(self) -> None:
        result = run("verify", "examples/ring_buffer.cpp", "--class", "Zebra")
        assert "RingBuffer" in result.stderr

    def test_a_file_with_no_classes_says_so(self) -> None:
        result = run("verify", "examples/off_by_one.cpp", "--class", "Foo")
        assert "no class" in result.stderr
