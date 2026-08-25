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
            r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M
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


class TestOutputSurvivesTheConsole:
    """veripp prints em dashes, and echoes identifiers from the user's source,
    so what it writes is not bounded by ASCII. A console encoding that cannot
    represent a character must not turn a finished verification into a
    traceback -- nor into UTF-8 bytes that the platform-default decoder in
    whatever captured it then chokes on.

    PYTHONIOENCODING reproduces a Windows codepage without a Windows machine.
    """

    def test_survives_a_codepage_missing_its_characters(self) -> None:
        import os
        import subprocess as sp
        import sys as _sys

        result = sp.run(
            [_sys.executable, "-m", "veripp.cli", "scan", "examples/ring_buffer.cpp"],
            capture_output=True, text=True, encoding="cp437", errors="replace",
            cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "cp437"}, timeout=1800,
        )
        assert "Scanned" in result.stdout, result.stderr[-400:]

    def test_output_is_readable_by_a_platform_default_decoder(self) -> None:
        """Forcing UTF-8 would fix the crash and break every tool capturing
        veripp with the platform default, which is the common case."""
        import os
        import subprocess as sp
        import sys as _sys

        result = sp.run(
            [_sys.executable, "-m", "veripp.cli", "--help"],
            capture_output=True, text=True, encoding="cp1252",
            cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "cp1252"}, timeout=600,
        )
        assert "verify" in result.stdout


class TestColour:
    """Colour on the verdict line, and nowhere it can leak.

    Every rule here is one other tools already follow, so nobody has to learn
    ours -- and every one of them is a way colour breaks someone's pipeline
    when it is missed.
    """

    @pytest.mark.esbmc
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

    @pytest.mark.esbmc
    def test_writes_the_report_and_keeps_readable_output(self, tmp_path) -> None:
        out = tmp_path / "r.json"
        result = run(
            "verify", "examples/off_by_one.cpp", "--function", "sum_array",
            "--json-out", str(out),
        )
        assert "Result: counterexample" in result.stdout
        import json

        assert json.loads(out.read_text(encoding="utf-8"))["outcome"]

    @pytest.mark.esbmc
    def test_works_for_scan_too(self, tmp_path) -> None:
        out = tmp_path / "s.json"
        run("scan", "examples/ring_buffer.cpp", "--json-out", str(out))
        import json

        assert json.loads(out.read_text(encoding="utf-8"))["candidates"]

    def test_the_action_verifies_only_once(self) -> None:
        """Two runs per job doubled the cost of every verification."""
        action = (ROOT / "action.yml").read_text(encoding="utf-8")
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

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="shell completions are for POSIX shells; git-bash on Windows is not where they are used",
    )
    def test_bash_script_is_valid_and_registers(self, tmp_path) -> None:
        import shutil
        import subprocess as sp

        if not shutil.which("bash"):
            pytest.skip("bash")
        path = tmp_path / "c.bash"
        path.write_text(run("completion", "bash").stdout, encoding="utf-8")
        assert sp.run(["bash", "-n", str(path)], capture_output=True).returncode == 0
        loaded = sp.run(
            ["bash", "-c", f"source {path} && complete -p veripp"],
            capture_output=True, text=True,
        )
        assert "_veripp" in loaded.stdout

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="shell completions are for POSIX shells; git-bash on Windows is not where they are used",
    )
    def test_zsh_script_is_valid(self, tmp_path) -> None:
        import shutil
        import subprocess as sp

        if not shutil.which("zsh"):
            pytest.skip("zsh")
        path = tmp_path / "c.zsh"
        path.write_text(run("completion", "zsh").stdout, encoding="utf-8")
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


class TestRoughInput:
    """What happens when the tool is pointed at something that is not a
    tidy C file. These are ordinary slips, and a crash is never the answer."""

    def test_a_directory_does_not_crash(self, tmp_path) -> None:
        """`veripp verify .` used to raise IsADirectoryError and exit 1."""
        (tmp_path / "a.c").write_text("int f(int x){return x;}\n", encoding="utf-8")
        result = run("verify", str(tmp_path), "--function", "f")
        assert "Traceback" not in result.stderr, result.stderr
        assert result.returncode == 2
        assert "is a directory" in result.stderr

    def test_a_directory_points_at_a_file_inside_it(self, tmp_path) -> None:
        (tmp_path / "parser.c").write_text("int f(int x){return x;}\n", encoding="utf-8")
        result = run("scan", str(tmp_path))
        assert "parser.c" in result.stderr

    def test_an_empty_file_says_it_is_empty(self, tmp_path) -> None:
        empty = tmp_path / "empty.c"
        empty.touch()
        result = run("verify", str(empty), "--function", "f")
        assert result.returncode == 2
        assert "is empty" in result.stderr

    def test_a_non_c_file_says_so(self, tmp_path) -> None:
        script = tmp_path / "script.py"
        script.write_text("def f(x):\n    return x\n", encoding="utf-8")
        result = run("verify", str(script), "--function", "f")
        assert result.returncode == 2
        assert "does not look like a C or C++ source file" in result.stderr

    def test_a_c_file_with_no_definitions_is_not_blamed_on_its_suffix(
        self, tmp_path
    ) -> None:
        header = tmp_path / "decls.h"
        header.write_text("int f(int x);\nint g(void);\n", encoding="utf-8")
        result = run("verify", str(header), "--function", "f")
        assert "does not look like" not in result.stderr
        assert "no C or C++ function definitions" in result.stderr

    def test_unparseable_c_does_not_crash(self, tmp_path) -> None:
        broken = tmp_path / "broken.c"
        broken.write_text("int broken(int x { return x;\n")
        result = run("verify", str(broken), "--function", "broken")
        assert "Traceback" not in result.stderr
        assert result.returncode in (1, 2, 3)


class TestScanNextStep:
    """A tally answers "what happened". The reader's question is "what now"."""

    def _scan(self, tmp_path, body: str):
        src = tmp_path / "m.c"
        src.write_text(body, encoding="utf-8")
        return run("scan", str(src))

    @pytest.mark.esbmc
    def test_counterexamples_get_a_runnable_command(self, tmp_path) -> None:
        out = self._scan(tmp_path, "int mean(int a,int b){ return (a+b)/2; }\n").stdout
        assert "next:" in out
        assert "veripp verify" in out and "--function mean" in out

    @pytest.mark.esbmc
    def test_it_says_a_counterexample_may_not_be_reachable(self, tmp_path) -> None:
        """The honest caveat: the input holds in the generated harness, which
        is not the same as a caller being able to produce it."""
        out = self._scan(tmp_path, "int mean(int a,int b){ return (a+b)/2; }\n").stdout
        assert "caller" in out

    @pytest.mark.esbmc
    def test_a_clean_scan_does_not_nag(self, tmp_path) -> None:
        body = "int clamp(int x){ if(x<0) return 0; if(x>100) return 100; return x; }\n"
        out = self._scan(tmp_path, body).stdout
        assert "next:" not in out, "nothing to do, so say nothing"


class TestBarePathJustWorks:
    """`veripp src/` means `veripp scan src/`.

    The premise of this tool is that it makes the decisions. "Which
    subcommand" is the first one a user should not have to make: pointed at
    code, the useful thing to do is check it.
    """

    @pytest.mark.esbmc
    def test_a_bare_directory_scans_it(self) -> None:
        result = run("examples/")
        assert result.returncode in (0, 1)
        assert "Scanned" in result.stdout, result.stderr[-300:]

    @pytest.mark.esbmc
    def test_a_bare_file_scans_it(self) -> None:
        result = run("examples/ring_buffer.cpp")
        assert result.returncode == 0
        assert "PROVED" in result.stdout + result.stderr

    def test_an_explicit_subcommand_is_untouched(self) -> None:
        assert "usage" in run("scan", "--help").stdout.lower()

    def test_a_typo_is_still_a_typo(self) -> None:
        """A mistyped command must not be mistaken for a missing path, or the
        did-you-mean disappears."""
        result = run("scna", "foo.c")
        assert result.returncode == 2
        assert "veripp scan" in result.stderr

    def test_a_path_that_does_not_exist_is_not_swallowed(self) -> None:
        result = run("no-such-file.c")
        assert result.returncode == 2
        assert "unknown command" in result.stderr or "not found" in result.stderr


class TestTheSurfaceAsksForAVerdict:
    """The default help must stay small enough to read.

    The user's objection to the previous design was that veripp exposed a
    configuration when it should ask for a verdict. Every ESBMC knob is still
    reachable via --help-all; what is bounded here is how much a newcomer has
    to read before they can run the tool.
    """

    def _flags(self, *argv):
        out = subprocess.run(
            [sys.executable, "-m", "veripp.cli", *argv],
            capture_output=True, text=True, cwd=ROOT,
        ).stdout
        return set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]+", out))

    @pytest.mark.parametrize("cmd", ["verify", "scan"])
    def test_default_help_is_short(self, cmd):
        shown = self._flags(cmd, "--help")
        assert len(shown) <= 16, (
            f"`veripp {cmd} --help` lists {len(shown)} flags; the default "
            "surface is meant to express intent, not configuration. Put "
            "tuning knobs behind _adv() so they appear in --help-all."
        )

    @pytest.mark.parametrize("cmd", ["verify", "scan"])
    def test_nothing_is_actually_removed(self, cmd):
        shown = self._flags(cmd, "--help")
        every = self._flags(cmd, "--help-all")
        assert every > shown, "--help-all must reveal strictly more"
        for knob in ("--unwind", "--timeout", "--std", "--compile-commands"):
            assert knob in every, f"{knob} must remain reachable"

    def test_hidden_flags_still_parse(self, tmp_path):
        # Hidden must mean "not advertised", never "not accepted".
        src = tmp_path / "t.c"
        src.write_text("int f(int x){ return x; }\n")
        out = subprocess.run(
            [sys.executable, "-m", "veripp.cli", "harness", str(src),
             "--function", "f", "--unwind", "4", "--max-array-len", "3"],
            capture_output=True, text=True, cwd=ROOT,
        )
        assert out.returncode == 0, out.stderr


class TestEveryEsbmcFeatureStaysReachable:
    """One escape hatch instead of a flag per check.

    The goal is access to the whole checker without a 30-flag surface, so
    --esbmc-arg forwards anything ESBMC accepts. The cost of that power is
    that a raw flag can weaken a proof (--no-bounds-check is one word), so
    the result line has to name whatever was passed.
    """

    def test_a_raw_flag_is_named_in_the_verdict(self):
        from dataclasses import replace
        from veripp.esbmc import VerifyConfig

        cfg = replace(VerifyConfig(), extra_args=["--no-bounds-check"])
        assert "--no-bounds-check" in cfg.describe(), (
            "a proof obtained under raw flags must say which ones, or "
            "'verified' hides that someone turned a check off"
        )

    def test_harness_plumbing_is_not_mistaken_for_a_check_flag(self):
        from dataclasses import replace
        from veripp.esbmc import VerifyConfig

        cfg = replace(VerifyConfig(), extra_args=["-I", "/usr/include", "-D", "X=1"])
        assert "raw ESBMC flags" not in cfg.describe()

    @pytest.mark.parametrize(
        "argv",
        [
            ["--esbmc-arg", "--struct-fields-check"],
            ["--esbmc-arg=--struct-fields-check"],
        ],
    )
    def test_both_spellings_work(self, tmp_path, argv):
        # `--esbmc-arg --flag` is what a person types; argparse alone reads
        # the second token as an option and rejects the first.
        src = tmp_path / "t.c"
        src.write_text("int f(int x){ return x; }\n")
        out = subprocess.run(
            [sys.executable, "-m", "veripp.cli", "harness", str(src),
             "--function", "f", *argv],
            capture_output=True, text=True, cwd=ROOT,
        )
        assert out.returncode == 0, out.stderr
