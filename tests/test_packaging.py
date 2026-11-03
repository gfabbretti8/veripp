"""The header ships with the package, or nothing works.

veripp puts `contracts.hpp` on ESBMC's include path at run time. When it was
kept outside the package it resolved fine from a source checkout and was
missing from every wheel, so `uv tool install` / `uvx` / `pip install` all
produced a tool that failed on its first real invocation. These tests pin the
guarantee that made that impossible.
"""

import subprocess
import sys
from pathlib import Path

import veripp
from veripp.paths import contracts_include_dir


def test_header_lives_inside_the_installed_package():
    packaged = Path(veripp.__file__).parent / "include" / "veripp" / "contracts.hpp"
    assert packaged.is_file(), (
        "contracts.hpp must live inside the veripp package so it ships in the "
        "wheel; see [tool.setuptools.package-data] in pyproject.toml"
    )


def test_include_dir_resolves_to_the_packaged_copy():
    include = contracts_include_dir()
    assert include is not None
    assert (include / "veripp" / "contracts.hpp").is_file()
    # This is the exact string handed to `esbmc -I`.
    assert include.is_dir()


def test_the_header_defines_what_generated_harnesses_use():
    header = (contracts_include_dir() / "veripp" / "contracts.hpp").read_text(encoding="utf-8")
    for macro in (
        "VERIPP_REQUIRES", "VERIPP_ENSURES", "VERIPP_ASSERT", "VERIPP_ASSUME",
        "VERIPP_NONDET_INT", "VERIPP_NONDET_UINT", "VERIPP_NONDET_LONG",
        "VERIPP_NONDET_ULONG", "VERIPP_NONDET_CHAR", "VERIPP_NONDET_BOOL",
        "VERIPP_NONDET_FLOAT", "VERIPP_NONDET_DOUBLE", "VERIPP_NONDET_SIZE",
        "VERIPP_HAS_OWN_MAIN",
    ):
        assert f"#define {macro}" in header, f"{macro} is used by generated harnesses"


def test_doctor_reports_the_header_as_found():
    proc = subprocess.run(
        [sys.executable, "-m", "veripp.cli", "doctor"],
        capture_output=True, text=True, cwd=Path(__file__).parent,
    )
    line = next(l for l in proc.stdout.splitlines() if l.startswith("contracts header:"))
    assert "NOT FOUND" not in line


class TestPyPIPage:
    """README.md becomes the PyPI project page.

    Relative links resolve on GitHub and 404 on PyPI, so they break only after
    publishing -- and only for the audience who found the project through PyPI.
    Both of the ones this catches were added while polishing the README.
    """

    def test_no_relative_links(self) -> None:
        import re
        from pathlib import Path

        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
        # Markdown links whose target is neither absolute nor an anchor.
        relative = [
            target
            for target in re.findall(r"\]\(([^)]+)\)", readme)
            if not target.startswith(("http://", "https://", "#", "mailto:"))
        ]
        assert not relative, (
            f"these README links 404 on the PyPI page: {relative}. "
            "Use absolute https://github.com/... URLs."
        )

    def test_the_metadata_points_somewhere_real(self) -> None:
        import re
        from pathlib import Path

        pyproject = (
            Path(__file__).resolve().parent.parent / "pyproject.toml"
        ).read_text(encoding="utf-8")
        for field in ("Homepage", "Repository", "Issues", "Changelog"):
            assert re.search(rf'^{field} = "https://', pyproject, re.M), (
                f"[project.urls] {field} is missing or not absolute"
            )

    def test_the_changelog_url_points_at_the_changelog(self) -> None:
        """It pointed at the commit log before CHANGELOG.md existed."""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        assert "CHANGELOG.md" in pyproject
        assert (root / "CHANGELOG.md").is_file()


class TestRequiresPython:
    """pyproject promises Python 3.10, so every module has to parse there.

    PEP 701 (Python 3.12) relaxed f-strings: an expression inside the braces
    may now reuse the enclosing quote or contain a backslash. On 3.10 and 3.11
    either one is a SyntaxError, and because it sits in llm.py -- imported by
    the CLI at start-up -- it took down every command, `veripp --version`
    included. CI runs 3.12, where nothing looks wrong. So on 3.12+ this reads
    each f-string back through the new tokenizer and checks that the old one
    would have accepted it too; on an older interpreter it simply compiles.
    """

    ROOT = Path(__file__).resolve().parent.parent
    DIRS = ("src", "checker", "tests", "scripts", "benchmarks", ".github")

    @staticmethod
    def _floor() -> tuple[int, int]:
        import re

        pyproject = (TestRequiresPython.ROOT / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        major, minor = re.search(
            r'^requires-python = ">=(\d+)\.(\d+)', pyproject, re.M
        ).groups()
        return int(major), int(minor)

    @staticmethod
    def _needs_pep701(text: str) -> list[int]:
        """Lines holding an f-string that only Python 3.12+ can parse."""
        import io
        import tokenize

        lines: list[int] = []
        depth, quote = 0, ""
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if depth == 0:
                if tok.type == tokenize.FSTRING_START:
                    quote = tok.string.lstrip("rRfFbBuU")
                    depth = 1
                continue
            if tok.type == tokenize.FSTRING_END and depth == 1:
                depth = 0
                continue
            # The literal text of the outer f-string may hold anything a
            # plain string can. Everything else -- the expressions, and
            # any string nested inside them -- was once part of a single
            # string token, so it could not contain the closing quote, and
            # could not contain a backslash at all.
            literal = tok.type == tokenize.FSTRING_MIDDLE and depth == 1
            if not literal and (quote in tok.string or "\\" in tok.string):
                lines.append(tok.start[0])
            if tok.type == tokenize.FSTRING_START:
                depth += 1
            elif tok.type == tokenize.FSTRING_END:
                depth -= 1
        return lines

    def test_every_module_parses_on_the_oldest_supported_python(self):
        import sys

        import pytest

        if self._floor() >= (3, 12):
            pytest.skip("requires-python no longer reaches below PEP 701")
        paths = [
            path
            for top in self.DIRS
            for path in sorted((self.ROOT / top).rglob("*.py"))
        ]
        assert paths
        offenders: dict[str, list[int]] = {}
        for path in paths:
            text = path.read_text(encoding="utf-8")
            if sys.version_info < (3, 12):
                compile(text, str(path), "exec")  # the old grammar itself
                continue
            lines = self._needs_pep701(text)
            if lines:
                offenders[str(path.relative_to(self.ROOT))] = lines
        assert not offenders, (
            "these f-strings need Python 3.12 (PEP 701) but requires-python "
            f"is >={'.'.join(map(str, self._floor()))}: {offenders}. Hoist "
            "the expression into a variable."
        )
