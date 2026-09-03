"""Counterexamples you can compile and run.

A trace asks the reader to trust that the harness modelled their function
fairly. A program that crashes asks for nothing. It also checks itself: a
repro that exits cleanly under the sanitizers is what a harness artifact
looks like from the outside.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from veripp.esbmc import Assignment
from veripp.repro import build_command, render

HARNESS = '''\
#define VERIPP_GENERATED_HARNESS 1
#include "veripp/contracts.hpp"
#include "/src/thing.c"

int main() {
    unsigned n = VERIPP_NONDET_UINT();
    VERIPP_ASSUME(n <= 4);
    int a_buf[4];
    for (unsigned long veripp_i = 0; veripp_i < 4; ++veripp_i)
        a_buf[veripp_i] = VERIPP_NONDET_INT();
    const int* a = a_buf;

    (void)sum_array(a, n);
    return 0;
}
'''

ASSIGNMENTS = [
    Assignment("n", "4", "n = 4"),
    Assignment("a_buf", "{ 0, 0, 0, 0 }", ""),
    Assignment("a_buf[0]", "-1", ""),
    Assignment("a_buf[3]", "-248", ""),
]


def _render(**kw) -> str:
    return render(HARNESS, Path("/src/thing.c"), "sum_array", ASSIGNMENTS, **kw)


class TestRendering:
    def test_nondeterminism_is_replaced_by_the_concrete_values(self):
        out = _render()
        assert "VERIPP_NONDET" not in out
        assert "unsigned n;" in out
        assert "n = 4;" in out and "a_buf[3] = -248;" in out

    def test_declarations_and_the_call_survive(self):
        out = _render()
        assert "int a_buf[4];" in out
        assert "const int* a = a_buf;" in out
        assert "(void)sum_array(a, n);" in out

    def test_the_assume_is_dropped(self):
        """It constrained a nondeterministic value that is now a literal."""
        assert "VERIPP_ASSUME" not in _render()

    def test_an_aggregate_becomes_a_comment_not_a_statement(self):
        """`a_buf = { 0, 0, 0, 0 }` is an initial state, not legal C as an
        assignment, and the element writes that follow supersede it."""
        out = _render()
        assert "a_buf = { 0, 0, 0, 0 };" not in out
        assert "//   a_buf = { 0, 0, 0, 0 }" in out

    def test_every_property_line_is_commented(self):
        """A property spans location, guard and CWEs. One uncommented line
        makes the file uncompilable."""
        out = _render(violated_property="boom\n  at /src/thing.c:7\n  CWE: CWE-125")
        body = out.split('#include "/src/thing.c"')[0]
        assert all(
            not line.strip() or line.startswith("//")
            for line in body.splitlines()
        ), body

    def test_it_says_what_a_clean_exit_means(self):
        assert "exits cleanly" in _render()


class TestBuildCommand:
    def test_c_sources_get_a_c_compiler(self):
        assert build_command(Path("r.c")).startswith("cc ")

    def test_cpp_sources_get_a_cpp_compiler(self):
        assert build_command(Path("r.cpp")).startswith("c++ ")

    def test_the_sanitizers_are_on(self):
        cmd = build_command(Path("r.c"))
        assert "-fsanitize=address,undefined" in cmd

    def test_include_paths_are_carried_over(self):
        """Without them the source's own `veripp/contracts.hpp` is not found
        and the reader blames the repro."""
        cmd = build_command(Path("r.c"), include_dirs=[Path("/inc/one"), None])
        assert "-I /inc/one" in cmd

    def test_it_names_the_file_that_was_written(self):
        assert "given/name.c" in build_command(Path("given/name.c"))


@pytest.mark.esbmc
@pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("c++"),
    reason="needs a POSIX C++ compiler with sanitizers",
)
def test_the_repro_actually_crashes(tmp_path):
    """End to end: verify a known bug, write the repro, build it with the
    printed command, and require the sanitizer to catch it. If this passes
    while the program exits 0, the feature is decorative."""
    root = Path(__file__).resolve().parent.parent
    out = tmp_path / "repro.cpp"
    verify = subprocess.run(
        [sys.executable, "-m", "veripp.cli", "verify",
         str(root / "examples/off_by_one.cpp"), "--function", "sum_array",
         "--no-llm", "--repro", str(out)],
        capture_output=True, text=True, cwd=root, timeout=600,
    )
    assert out.is_file(), verify.stderr

    build = next(
        line.split("build:", 1)[1].strip()
        for line in verify.stderr.splitlines() if "build:" in line
    )
    compiled = subprocess.run(build + f" -o {tmp_path / 'repro'}",
                              shell=True, capture_output=True, text=True, cwd=tmp_path)
    assert compiled.returncode == 0, compiled.stderr

    ran = subprocess.run([str(tmp_path / "repro")], capture_output=True, text=True)
    assert ran.returncode != 0, "the repro exited cleanly; it reproduces nothing"
    assert "sum_array" in ran.stderr
