"""Whole-file scanning: point veripp at a file, not a function."""

import pytest

from veripp.cppsig import find_struct, function_definitions
from veripp.esbmc import VerifyConfig
from veripp.harness import HarnessOptions, generate
from veripp.paths import contracts_include_dir
from veripp.scan import scan

SOURCE = """\
#include "veripp/contracts.hpp"

/* The dominant C idiom: an anonymous struct behind a typedef. */
typedef struct {
    int         len;
    unsigned char* data;
} buf_t;

static int safe_add(int a, int b) {
    VERIPP_REQUIRES(a > -1000 && a < 1000);
    VERIPP_REQUIRES(b > -1000 && b < 1000);
    return a + b;
}

static int unsafe_div(int a, int b) { return a / b; }

static int buf_len(buf_t* b) { return b->len; }

int declared_elsewhere(int x);
"""


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "s.c"
    p.write_text(SOURCE)
    return p


class TestEnumeration:
    def test_finds_definitions_but_not_declarations(self):
        names = function_definitions(SOURCE)
        assert {"safe_add", "unsafe_div", "buf_len"} <= set(names)
        assert "declared_elsewhere" not in names

    def test_anonymous_typedef_struct_is_resolved(self):
        """`typedef struct { ... } buf_t;` has no struct name for the scanner
        to key on, and most C libraries declare every type this way."""
        info = find_struct(SOURCE, "buf_t")
        assert [f.name for f in info.fields] == ["len", "data"]

    def test_field_types_are_single_line(self):
        """scrub() blanks comments but keeps their newlines, so a multi-line
        comment inside a declaration used to leak one into the field type --
        and from there into a `//` assumption block, emitting invalid C++."""
        src = "struct S {\n    int /* a\n       multi-line comment */ n;\n};\n"
        assert find_struct(src, "S").fields[0].type == "int"


class TestAssumptionRendering:
    def test_assumptions_never_break_the_comment_block(self, tmp_path):
        p = tmp_path / "s.c"
        p.write_text(
            '#include "veripp/contracts.hpp"\n'
            "struct S { int /* multi\nline */ n; };\n"
            "static int f(struct S* s) { return s->n; }\n"
        )
        header = generate(p, "f").code.split("int main")[0]
        for line in header.splitlines():
            assert line == "" or line.startswith(("//", "#", "int", "struct")) or "#" in line


@pytest.mark.esbmc
class TestScanEndToEnd:
    def _scan(self, src, **kw):
        # The harness is always C++, even for a .c target: it #includes the
        # source and needs `extern "C"` and reference parameters.
        config = VerifyConfig(
            timeout_s=90,
            include_dirs=[contracts_include_dir(), src.parent],
        )
        return scan(src, config, HarnessOptions(), jobs=4, **kw)

    def test_separates_proofs_from_counterexamples(self, src):
        report = self._scan(src)
        proved = {r.name for r in report.proved}
        broken = {r.name for r in report.counterexamples}
        assert "safe_add" in proved          # its own REQUIRES bound it
        assert "unsafe_div" in broken        # nothing constrains b
        assert not (proved & broken)

    def test_struct_parameters_are_harnessed_not_refused(self, src):
        report = self._scan(src)
        assert "buf_len" not in {r.name for r in report.refused}

    def test_summary_reports_why_things_were_refused(self, src):
        report = self._scan(src)
        text = report.summary()
        assert "PROVED" in text and "NOT HARNESSABLE" in text
        assert str(report.candidates) in text

    def test_main_is_not_a_target(self, tmp_path):
        p = tmp_path / "m.c"
        p.write_text("int main(void) { return 0; }\nstatic int f(int x) { return x; }\n")
        assert "main" not in {r.name for r in self._scan(p).results}


class TestErrorGuidance:
    """A refusal should point at what the file does offer."""

    def _run(self, capsys, src, function):
        from veripp.cli import main

        main(["verify", str(src), "--function", function, "--no-llm"])
        return capsys.readouterr().err

    def test_a_typo_suggests_the_right_name(self, capsys, src):
        err = self._run(capsys, src, "safe_ad")
        assert "did you mean" in err and "safe_add" in err

    def test_an_unrelated_name_lists_what_exists(self, capsys, src):
        err = self._run(capsys, src, "zzzzzz")
        assert "this file defines" in err
        assert "safe_add" in err

    def test_it_points_at_scan(self, capsys, src):
        assert "veripp scan" in self._run(capsys, src, "zzzzzz")
