"""Whole-file scanning: point veripp at a file, not a function."""

from pathlib import Path

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


@pytest.mark.esbmc
class TestScanEscalation:
    """`scan` must not answer "inconclusive" where `verify` would answer.

    `verify` widens the unwind bound when a run exhausts it. `scan` did not,
    so the two commands disagreed about the same function -- 41 of lodepng's
    inconclusive results were a bound `verify` would have widened.
    """

    # unwind 2 exhausts the bound; one escalation to 8 clears a loop of 6.
    SRC = (
        '#include "veripp/contracts.hpp"\n'
        "static int walk(unsigned n) {\n"
        "    int s = 0;\n"
        "    for (unsigned i = 0; i < n && i < 6; ++i) s += 1;\n"
        "    return s;\n"
        "}\n"
    )

    def _scan(self, tmp_path, escalations):
        from veripp.scan import scan

        p = tmp_path / "s.c"
        p.write_text(self.SRC)
        config = VerifyConfig(
            timeout_s=90, unwind=2,
            include_dirs=[contracts_include_dir(), p.parent],
        )
        return scan(p, config, HarnessOptions(), jobs=1, escalations=escalations)

    def test_without_escalation_the_bound_is_hit(self, tmp_path):
        outcomes = {r.name: r.outcome for r in self._scan(tmp_path, 0).results}
        assert outcomes["walk"] == "unwind_limit"

    def test_with_escalation_it_settles(self, tmp_path):
        outcomes = {r.name: r.outcome for r in self._scan(tmp_path, 1).results}
        assert outcomes["walk"] != "unwind_limit"


class TestAdviceIsTimely:
    def test_no_llm_hint_on_a_proof(self, capsys, src, monkeypatch):
        """Telling someone their counterexamples will not be triaged is advice
        about a problem they do not have when the answer is 'verified'."""
        from veripp.cli import main

        for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "VERIPP_LLM_MODEL",
                    "VERIPP_LLM_BASE_URL"):
            monkeypatch.delenv(var, raising=False)
        main(["verify", str(src), "--function", "safe_add", "--timeout", "90"])
        assert "no LLM configured" not in capsys.readouterr().err

    def test_the_hint_appears_when_there_is_something_to_triage(
        self, capsys, src, monkeypatch
    ):
        from veripp.cli import main

        for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "VERIPP_LLM_MODEL",
                    "VERIPP_LLM_BASE_URL"):
            monkeypatch.delenv(var, raising=False)
        main(["verify", str(src), "--function", "unsafe_div", "--timeout", "90"])
        assert "no LLM configured" in capsys.readouterr().err


class TestHarnessLanguage:
    """A C file needs a C harness.

    `T *p = malloc(...)` is idiomatic C and invalid C++, so compiling a C
    source through a C++ harness fails outright. tinyexpr scored 45 of 47
    functions "inconclusive" for exactly this reason, and every one was a
    parse error rather than anything about the code.
    """

    def test_a_c_source_gets_a_c_harness(self, tmp_path):
        from veripp.harness import generate

        p = tmp_path / "s.c"
        p.write_text(
            '#include "veripp/contracts.hpp"\n'
            "#include <stdlib.h>\n"
            "typedef struct { int n; } box_t;\n"
            "static box_t* make(void) { box_t* b = malloc(sizeof(box_t)); return b; }\n"
        )
        assert generate(p, "make").write(tmp_path / "out").suffix == ".c"

    def test_a_cpp_source_gets_a_cpp_harness(self, tmp_path):
        from veripp.harness import generate

        p = tmp_path / "s.cpp"
        p.write_text('#include "veripp/contracts.hpp"\nint f(int x) { return x; }\n')
        assert generate(p, "f").write(tmp_path / "out").suffix == ".cpp"

    def test_the_standard_follows_the_harness_language(self):
        from veripp.esbmc import VerifyConfig

        config = VerifyConfig()
        assert config.std_for(Path("h.c")) == "c11"
        assert config.std_for(Path("h.cpp")) == "c++17"
        assert "c11" in config.to_args(Path("h.c"))
        assert "c++17" in config.to_args(Path("h.cpp"))


def test_contracts_header_is_usable_from_c():
    """It is included by C and C++ harnesses alike, so `extern "C"` and `bool`
    have to be guarded."""
    from veripp.paths import contracts_include_dir

    text = (contracts_include_dir() / "veripp" / "contracts.hpp").read_text()
    assert "#if defined(__cplusplus)" in text
    assert "_Bool" in text
    # the brace must be inside the guard, not produced by a macro
    assert 'extern "C" {' in text
