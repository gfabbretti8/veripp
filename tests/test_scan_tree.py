"""Scanning a directory.

Every neighbouring tool -- ripgrep, fd, clang-tidy -- takes a directory, and
requiring one file at a time is the difference between working on a file and
working on a project.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def run(*args: str, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "veripp.cli", *args],
        capture_output=True, text=True, cwd=cwd or ROOT, timeout=1800,
    )


class TestDiscovery:
    """Which files get scanned, decided without running the checker."""

    def test_finds_sources_recursively(self, tmp_path) -> None:
        from veripp.cli import discover_sources

        (tmp_path / "a.c").touch()
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.cpp").touch()
        found = {p.name for p in discover_sources(tmp_path)}
        assert found == {"a.c", "b.cpp"}

    def test_skips_build_and_vendor_trees(self, tmp_path) -> None:
        """Scanning a vendored dependency reports findings nobody owns."""
        from veripp.cli import discover_sources

        (tmp_path / "real.c").touch()
        for junk in ("build", "node_modules", "third_party", ".git"):
            (tmp_path / junk).mkdir()
            (tmp_path / junk / "x.c").touch()
        assert {p.name for p in discover_sources(tmp_path)} == {"real.c"}

    def test_skips_headers(self, tmp_path) -> None:
        """Definitions live in the source file; scanning both reports the same
        functions twice and doubles the work."""
        from veripp.cli import discover_sources

        (tmp_path / "a.c").touch()
        (tmp_path / "a.h").touch()
        assert {p.name for p in discover_sources(tmp_path)} == {"a.c"}

    def test_order_is_stable(self, tmp_path) -> None:
        """A scan whose output reorders between runs cannot be diffed."""
        from veripp.cli import discover_sources

        for name in ("z.c", "a.c", "m.cpp"):
            (tmp_path / name).touch()
        assert discover_sources(tmp_path) == discover_sources(tmp_path)
        assert [p.name for p in discover_sources(tmp_path)] == ["a.c", "m.cpp", "z.c"]


class TestTreeScan:
    @pytest.mark.esbmc
    def test_aggregates_across_files(self, tmp_path) -> None:
        (tmp_path / "good.c").write_text(
            "int clamp(int x){ if(x<0) return 0; if(x>100) return 100; return x; }\n"
        , encoding="utf-8")
        (tmp_path / "bad.c").write_text("int mean(int a,int b){ return (a+b)/2; }\n", encoding="utf-8")
        result = run("scan", str(tmp_path))
        assert result.returncode == 1, "a tree containing a finding must exit 1"
        assert "Scanned 2 files" in result.stdout
        assert "bad.c" in result.stdout and "mean" in result.stdout

    @pytest.mark.esbmc
    def test_a_clean_tree_exits_zero(self, tmp_path) -> None:
        (tmp_path / "good.c").write_text(
            "int clamp(int x){ if(x<0) return 0; if(x>100) return 100; return x; }\n"
        , encoding="utf-8")
        assert run("scan", str(tmp_path)).returncode == 0

    @pytest.mark.esbmc
    def test_json_reports_every_file(self, tmp_path) -> None:
        (tmp_path / "a.c").write_text("int f(int x){ return x; }\n", encoding="utf-8")
        (tmp_path / "b.c").write_text("int g(int x){ return x; }\n", encoding="utf-8")
        payload = json.loads(run("scan", str(tmp_path), "--json").stdout)
        assert payload["files"] == 2
        assert {Path(e["file"]).name for e in payload["per_file"]} == {"a.c", "b.c"}

    def test_an_empty_tree_explains_itself(self, tmp_path) -> None:
        (tmp_path / "notes.txt").write_text("nothing to verify here\n", encoding="utf-8")
        result = run("scan", str(tmp_path))
        assert result.returncode == 2
        assert "no C or C++ source files" in result.stderr
        assert ".c" in result.stderr, "say what it looked for"

    @pytest.mark.esbmc
    def test_one_bad_file_does_not_lose_the_others(self, tmp_path) -> None:
        """A tree scan that aborts on the first unreadable file wastes
        everything it had already done."""
        (tmp_path / "ok.c").write_text("int f(int x){ return x; }\n", encoding="utf-8")
        broken = tmp_path / "broken.c"
        broken.write_bytes(b"\xff\xfe\x00 not really source \x00")
        result = run("scan", str(tmp_path))
        assert "Scanned" in result.stdout


class TestVerifyStillWantsAFile:
    def test_verify_on_a_directory_points_at_scan(self) -> None:
        result = run("verify", "examples", "--function", "f")
        assert result.returncode == 2
        assert "veripp scan examples" in result.stderr


@pytest.mark.esbmc
class TestTreeWithCompilationDatabase:
    """The real-project path: a source tree plus compile_commands.json.

    Everything derived from the source has to be derived per file. A database
    is keyed by translation unit, and the harness's include path starts at the
    file's own directory -- neither means anything for a directory.
    """

    @staticmethod
    def _project(tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "inc").mkdir()
        (tmp_path / "build").mkdir()
        (tmp_path / "inc" / "cfg.h").write_text("#define LIMIT 4\n", encoding="utf-8")
        (tmp_path / "src" / "a.c").write_text(
            '#include "cfg.h"\n'
            "int at(const int*a,int i){ if(i<0||i>=LIMIT) return 0; return a[i]; }\n"
        , encoding="utf-8")
        (tmp_path / "build" / "compile_commands.json").write_text(json.dumps([{
            "directory": str(tmp_path / "build"),
            "file": str(tmp_path / "src" / "a.c"),
            "command": f"cc -I{tmp_path / 'inc'} -c {tmp_path / 'src' / 'a.c'}",
        }]))
        return tmp_path

    def test_the_database_reaches_each_file(self, tmp_path) -> None:
        """Resolving once against the tree root looked the *directory* up in
        the database, which matches nothing: the scan died with a usage error
        before verifying anything."""
        project = self._project(tmp_path)
        result = run("scan", "src", "--compile-commands",
                     "build/compile_commands.json", cwd=project)
        assert result.returncode == 0, result.stderr[-800:]
        assert "not in" not in result.stderr, result.stderr[-400:]
        assert "PROVED" in result.stderr or "PROVED" in result.stdout

    def test_a_file_outside_the_database_does_not_abort_the_run(self, tmp_path) -> None:
        """A tree legitimately contains files no database covers -- tests,
        fuzzers, generated code. Losing the whole scan over one is wrong."""
        project = self._project(tmp_path)
        (project / "src" / "extra.c").write_text(
            "int helper(int x){ if(x<0) return 0; return x; }\n"
        , encoding="utf-8")
        result = run("scan", "src", "--compile-commands",
                     "build/compile_commands.json", cwd=project)
        assert result.returncode == 0, result.stderr[-800:]
        assert "Scanned 2 files" in result.stdout

    def test_a_single_file_still_fails_loudly_on_a_bad_database(self, tmp_path) -> None:
        """Skipping is right for one file inside a tree, not for the single
        file someone explicitly named."""
        project = self._project(tmp_path)
        (project / "src" / "extra.c").write_text("int helper(int x){ return x; }\n", encoding="utf-8")
        result = run("verify", "src/extra.c", "--function", "helper",
                     "--compile-commands", "build/compile_commands.json", cwd=project)
        assert result.returncode == 2
        assert "not in" in result.stderr


class TestOnlyFilter:
    """`--only` turns a twenty-minute scan into a five-second one while
    iterating on a single area of a codebase."""

    @staticmethod
    def _file(tmp_path):
        (tmp_path / "m.c").write_text(
            "int parse_header(int x){ return x; }\n"
            "int parse_body(int x){ return x; }\n"
            "int write_out(int x){ return x; }\n"
        , encoding="utf-8")
        return tmp_path

    @pytest.mark.esbmc
    def test_a_glob_selects_a_subset(self, tmp_path) -> None:
        result = run("scan", "m.c", "--only", "parse_*", cwd=self._file(tmp_path))
        assert result.returncode == 0
        assert "2 function definitions" in result.stdout, result.stdout[-400:]

    @pytest.mark.esbmc
    def test_globs_are_repeatable(self, tmp_path) -> None:
        result = run("scan", "m.c", "--only", "parse_header", "--only", "write_*",
                     cwd=self._file(tmp_path))
        assert "2 function definitions" in result.stdout

    @pytest.mark.esbmc
    def test_an_exact_name_works(self, tmp_path) -> None:
        result = run("scan", "m.c", "--only", "write_out", cwd=self._file(tmp_path))
        assert "1 function definition" in result.stdout

    def test_a_glob_matching_nothing_is_an_error(self, tmp_path) -> None:
        """Silently scanning everything after a typo'd glob would be worse
        than failing: the user would think they had checked one function and
        actually have checked all of them, or none."""
        result = run("scan", "m.c", "--only", "nope_*", cwd=self._file(tmp_path))
        assert result.returncode == 2
        assert "matched no function" in result.stderr

    @pytest.mark.esbmc
    def test_across_a_tree_a_file_with_no_match_is_skipped(self, tmp_path) -> None:
        """Not an error there: most files in a tree legitimately contain
        nothing matching."""
        (tmp_path / "a.c").write_text("int parse_one(int x){ return x; }\n", encoding="utf-8")
        (tmp_path / "b.c").write_text("int unrelated(int x){ return x; }\n", encoding="utf-8")
        result = run("scan", str(tmp_path), "--only", "parse_*", cwd=tmp_path)
        assert result.returncode == 0
        assert "Scanned 1 file" in result.stdout, result.stdout[-400:]


class TestScanAsksAboutTerminationToo:
    """`verify` and `scan` must not disagree about a function.

    A bare path dispatches to `scan`, so if termination only appeared in
    `verify` most people would never see it.
    """

    def test_a_loop_is_asked_and_a_straight_line_is_not(self, tmp_path):
        src = tmp_path / "m.c"
        src.write_text(
            "unsigned s(unsigned n){ unsigned t=0; "
            "for(unsigned i=0;i<n&&i<8;i++) t+=i; return t; }\n"
            "int p(int x){ return x & 0xff; }\n"
        )
        from veripp.esbmc import VerifyConfig
        from veripp.paths import contracts_include_dir
        from veripp.scan import scan
        from dataclasses import replace

        inc = [d for d in (contracts_include_dir(),) if d]
        report = scan(src, replace(VerifyConfig(), include_dirs=inc), jobs=2)
        by = {r.name: r for r in report.results}
        assert by["s"].terminates is True
        assert by["p"].terminates is None, (
            "a function with no loop should not be charged an extra "
            "verification run to prove the obvious"
        )

    def test_body_scan_ignores_loops_in_other_functions(self):
        # File-wide detection would charge every proved function in a file
        # for one loop anywhere in it.
        from veripp.cppsig import scrub
        from veripp.scan import _body_has_loop

        text = scrub(
            "int quiet(int x){ return x; }\n"
            "int busy(int n){ while(n--){} return 0; }\n"
        )
        assert _body_has_loop(text, "busy") is True
        assert _body_has_loop(text, "quiet") is False

    def test_declaration_is_not_mistaken_for_a_definition(self):
        from veripp.cppsig import scrub
        from veripp.scan import _body_has_loop

        text = scrub("int f(int);\nint g(int n){ while(n--){} return 0; }\n")
        assert _body_has_loop(text, "f") is False
