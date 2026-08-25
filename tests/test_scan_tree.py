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
        )
        (tmp_path / "bad.c").write_text("int mean(int a,int b){ return (a+b)/2; }\n")
        result = run("scan", str(tmp_path))
        assert result.returncode == 1, "a tree containing a finding must exit 1"
        assert "Scanned 2 files" in result.stdout
        assert "bad.c" in result.stdout and "mean" in result.stdout

    @pytest.mark.esbmc
    def test_a_clean_tree_exits_zero(self, tmp_path) -> None:
        (tmp_path / "good.c").write_text(
            "int clamp(int x){ if(x<0) return 0; if(x>100) return 100; return x; }\n"
        )
        assert run("scan", str(tmp_path)).returncode == 0

    @pytest.mark.esbmc
    def test_json_reports_every_file(self, tmp_path) -> None:
        (tmp_path / "a.c").write_text("int f(int x){ return x; }\n")
        (tmp_path / "b.c").write_text("int g(int x){ return x; }\n")
        payload = json.loads(run("scan", str(tmp_path), "--json").stdout)
        assert payload["files"] == 2
        assert {Path(e["file"]).name for e in payload["per_file"]} == {"a.c", "b.c"}

    def test_an_empty_tree_explains_itself(self, tmp_path) -> None:
        (tmp_path / "notes.txt").write_text("nothing to verify here\n")
        result = run("scan", str(tmp_path))
        assert result.returncode == 2
        assert "no C or C++ source files" in result.stderr
        assert ".c" in result.stderr, "say what it looked for"

    @pytest.mark.esbmc
    def test_one_bad_file_does_not_lose_the_others(self, tmp_path) -> None:
        """A tree scan that aborts on the first unreadable file wastes
        everything it had already done."""
        (tmp_path / "ok.c").write_text("int f(int x){ return x; }\n")
        broken = tmp_path / "broken.c"
        broken.write_bytes(b"\xff\xfe\x00 not really source \x00")
        result = run("scan", str(tmp_path))
        assert "Scanned" in result.stdout


class TestVerifyStillWantsAFile:
    def test_verify_on_a_directory_points_at_scan(self) -> None:
        result = run("verify", "examples", "--function", "f")
        assert result.returncode == 2
        assert "veripp scan examples" in result.stderr
