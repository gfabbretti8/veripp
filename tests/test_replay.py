"""Replaying a counterexample natively, and leaks that are really leaks.

Two things the field report's ratio -- ten real defects to thirty-two false
ones -- asked for. The first is a way to sort counterexamples that needs no
judgement: build the inputs into a program, run it under the sanitizers, and
say what the compiled function did. The second came out of testing the
first: a function that mallocs, returns early and frees nothing was reported
*verified*, because the checker files every leak where the program ends,
that is in the harness, and failures in the harness were discarded.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from veripp.esbmc import parse_output
from veripp.harness import generate
from veripp.repro import Replay, render, replay, replay_counterexample

needs_cc = pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")


class _A:
    def __init__(self, lvalue, value):
        self.lvalue, self.value = lvalue, value


HARNESS = """\
#include "x.c"

#if !defined(__ESBMC__)
#error "veripp harnesses are built by ESBMC only (it is invoked with -D__ESBMC__)"
#endif

static int veripp_stub_param_fn(void *ctx, int v) { return VERIPP_NONDET_INT(); }

int main() {
    char p_buf[4];
    for (unsigned long veripp_i = 0; veripp_i < 4; ++veripp_i)
        p_buf[veripp_i] = VERIPP_NONDET_CHAR();
    unsigned long end_offset = VERIPP_NONDET_ULONG();
    VERIPP_ASSUME(end_offset <= 4);
    const char * p = p_buf;

    (void)parse(p, end_offset);
    VERIPP_REACHED();
    return 0;
}
"""


class TestTheRepro:
    def test_array_contents_are_inputs_not_a_comment(self):
        """Four digits left in a comment meant a parser that over-reads on
        digits was handed four zeroes, stopped at the first, and 'ran clean'."""
        out = render(HARNESS, Path("/src/x.c"), "parse",
                     [_A("p_buf", "{ 56, 56, 56, 56 }"), _A("end_offset", "4")])
        for index in range(4):
            assert f"p_buf[{index}] = 56;" in out
        assert out.index("p_buf[3] = 56;") < out.index("(void)parse(")

    def test_what_the_trace_does_not_mention_is_zero(self):
        out = render(HARNESS, Path("/src/x.c"), "parse", [])
        assert "char p_buf[4] = {0};" in out
        assert "unsigned long end_offset = 0;" in out

    def test_callback_stand_ins_come_along(self):
        out = render(HARNESS, Path("/src/x.c"), "parse", [])
        assert "static int veripp_stub_param_fn(void *ctx, int v) { return 0; }" in out
        assert "VERIPP_NONDET" not in out

    def test_a_source_with_its_own_main_is_set_aside_here_too(self):
        harness = HARNESS.replace(
            '#include "x.c"\n',
            '#define main veripp_original_main\n#include "x.c"\n#undef main\n')
        out = render(harness, Path("/src/x.c"), "parse", [])
        assert ('#define main veripp_original_main\n#include "/src/x.c"\n'
                "#undef main") in out


@needs_cc
class TestReplay:
    def _run(self, tmp_path, body, prop=""):
        return replay(f"#include <stdlib.h>\n#include <string.h>\n"
                      f"int main(void) {{ {body} return 0; }}\n", ".c", prop)

    def test_an_overflow_the_sanitizer_sees_is_confirmed(self, tmp_path):
        got = self._run(tmp_path, "volatile int n = 4; char b[4]; b[n] = 1;")
        assert got.status == "crashed"
        assert "AddressSanitizer" in got.detail
        assert "CONFIRMED" in got.summary()[0]

    def test_undefined_arithmetic_is_confirmed(self, tmp_path):
        got = self._run(tmp_path, "volatile int v = 2147483647; v = v * 2;")
        assert got.status == "crashed"
        assert "signed integer overflow" in got.detail

    def test_a_clean_run_is_doubt_not_refutation(self, tmp_path):
        got = self._run(tmp_path, "char b[4]; b[3] = 1;")
        assert got.status == "clean"
        text = " ".join(got.summary())
        assert "doubtful" in text and "not as refuted" in text

    def test_a_property_no_sanitizer_checks_says_so(self, tmp_path):
        got = self._run(tmp_path, "double d = 0.0; d = d / d;", prop="NaN on ieee_div")
        assert got.status == "unchecked"
        assert "says nothing" in got.summary()[0]

    def test_a_program_that_does_not_build_is_no_answer(self, tmp_path):
        got = replay("int main(void) { return undeclared; }\n", ".c")
        assert got.status == "not_built"
        assert "Not replayed" in got.summary()[0]

    def test_a_trace_without_inputs_is_not_run_on_zeroes(self):
        got = replay_counterexample(HARNESS, Path("/src/x.c"), "parse", [])
        assert got.status == "unchecked"
        assert "nothing concrete to run" in got.detail

    def test_a_sequence_harness_is_not_replayed(self):
        got = replay_counterexample("int main() { int veripp_step = 0; }",
                                    Path("/src/x.c"), "f", [])
        assert got.status == "not_built"


HEAP = """\
#include <stdlib.h>
#include <string.h>
struct obj { char *name; int n; };
typedef struct node { struct node *next; int v; } node;

void name_set(struct obj *o) { o->name = malloc(4); }
char *make_buf(int n) { return malloc(n > 0 ? 8 : 4); }
node *push(node *head, int v) {
    node *n = malloc(sizeof *n);
    if (!n) return head;
    n->next = head; n->v = v;
    return n;
}
int roundtrip(int n) {
    char *p = malloc(8);
    if (!p) return -1;
    p[0] = (char)n; n = p[0]; free(p);
    return n;
}
void name_swap(struct obj *o) { o->name = malloc(4); o->name = malloc(8); }
int dup_len(const char *s) {
    char *copy = malloc(strlen(s) + 1);
    if (!copy) return -1;
    strcpy(copy, s);
    if (copy[0] == '#') return 0;
    int n = (int)strlen(copy);
    free(copy);
    return n;
}
"""


class TestLeakRoots:
    def test_the_harness_keeps_what_a_caller_still_holds(self, tmp_path):
        src = tmp_path / "heap.c"
        src.write_text(HEAP, encoding="utf-8")
        code = generate(src, "name_set").code
        assert "static struct obj veripp_kept_o_obj;" in code
        assert "veripp_kept_o_obj = o_obj;" in code
        assert code.index("name_set(o);") < code.index("veripp_kept_o_obj = o_obj;")

    def test_a_returned_pointer_is_kept(self, tmp_path):
        src = tmp_path / "heap.c"
        src.write_text(HEAP, encoding="utf-8")
        code = generate(src, "make_buf").code
        assert "veripp_kept_result = (const void *)make_buf(n);" in code
        assert "(void)make_buf" not in code

    def test_a_file_that_cannot_allocate_is_left_alone(self, tmp_path):
        src = tmp_path / "pure.c"
        src.write_text("struct p { int x; };\nint get(struct p *p) { return p->x; }\n",
                       encoding="utf-8")
        assert "veripp_kept_" not in generate(src, "get").code

    def test_the_allocation_site_is_read_from_the_trace(self):
        output = (
            "[Counterexample]\n\n"
            "State 1 file /src/heap.c line 22 column 5 function dup_len thread 0\n"
            "----------------------------------------------------\n"
            "  copy = &dynamic_1_array[0]\n\n"
            "State 20 file /tmp/v/veripp_harness_dup_len.c line 33 column 1 function main thread 0\n"
            "----------------------------------------------------\n"
            "Violated property:\n"
            "  file /tmp/v/veripp_harness_dup_len.c line 33 column 1 function main\n"
            "  dereference failure: forgotten memory: dynamic_1_array\n"
            "  CWE: CWE-401\n\n\n"
            "VERIFICATION FAILED\n"
        )
        prop = parse_output(output, 0).violated_property
        assert prop.allocated_at is not None
        assert (prop.allocated_at.file, prop.allocated_at.line) == ("/src/heap.c", 22)
        assert prop.user_loc.line == 22

    @pytest.mark.esbmc
    def test_real_leaks_are_found_and_handed_back_memory_is_not(self, tmp_path):
        from veripp.esbmc import VerifyConfig
        from veripp.harness import HarnessOptions
        from veripp.paths import contracts_include_dir
        from veripp.scan import scan

        src = tmp_path / "heap.c"
        src.write_text(HEAP, encoding="utf-8")
        config = VerifyConfig(timeout_s=120, include_dirs=[contracts_include_dir()])
        report = scan(src, config, HarnessOptions(), jobs=2, retry_budget=0)
        leaked = {r.name for r in report.counterexamples}
        proved = {r.name for r in report.proved}
        assert leaked == {"name_swap", "dup_len"}, report.summary()
        assert {"name_set", "make_buf", "push", "roundtrip"} <= proved, report.summary()
        assert not report.artifacts, report.summary()
        # Filed where the memory was allocated, not at the harness's brace.
        at = {r.name: (Path(r.file).name, r.line) for r in report.counterexamples}
        assert at["dup_len"] == ("heap.c", 22)

    @pytest.mark.esbmc
    def test_a_leak_is_no_longer_verified(self, tmp_path, capsys):
        from veripp.cli import main

        src = tmp_path / "heap.c"
        src.write_text(HEAP, encoding="utf-8")
        assert main(["verify", str(src), "--function", "dup_len", "--no-llm"]) == 1
        out = capsys.readouterr().out
        assert "forgotten memory" in out
        assert "allocated at" in out and "out of reach when the call returns" in out


@needs_cc
@pytest.mark.esbmc
class TestEndToEnd:
    def test_verify_replay_confirms_a_real_overflow(self, tmp_path, capsys):
        from veripp.cli import main

        src = tmp_path / "r.c"
        src.write_text(
            "int parse_u8(const char *p, const char *end) {\n"
            "    int v = 0;\n"
            "    while (*p >= '0' && *p <= '9' && p < end) {\n"
            "        v = (v * 10 + (*p - '0')) & 0xff; p++;\n"
            "    }\n"
            "    return v;\n"
            "}\n", encoding="utf-8")
        assert main(["verify", str(src), "--function", "parse_u8", "--no-llm",
                     "--replay"]) == 1
        out = capsys.readouterr().out
        assert "Replayed natively: CONFIRMED. AddressSanitizer" in out
        assert "the line the checker named" in out

    def test_scan_replay_lists_confirmed_findings_first(self, tmp_path):
        from veripp.esbmc import VerifyConfig
        from veripp.harness import HarnessOptions
        from veripp.paths import contracts_include_dir
        from veripp.scan import ranked_counterexamples, scan

        src = tmp_path / "m.c"
        src.write_text(
            "void fill(char *dst, unsigned n) {\n"
            "    for (unsigned i = 0; i <= n; i++) dst[i] = 'x';\n}\n"
            "double ratio(double a, double b) { return a / b - a / b; }\n",
            encoding="utf-8")
        config = VerifyConfig(timeout_s=120, include_dirs=[contracts_include_dir()])
        report = scan(src, config, HarnessOptions(), jobs=2, retry_budget=0,
                      replay=True)
        by_name = {r.name: r for r in report.counterexamples}
        assert by_name["fill"].replay == "crashed", report.summary()
        assert ranked_counterexamples(report.counterexamples)[0].name == "fill"
        assert "CONFIRMED natively" in report.summary()


def test_replay_summary_is_plain_about_no_compiler():
    assert "no C or C++ compiler" in Replay("no_compiler").summary()[0]
