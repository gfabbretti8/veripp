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

    def test_a_clean_run_on_no_inputs_is_not_doubt(self, tmp_path):
        """For a leak the trace names the allocation and no input values."""
        src = tmp_path / "x.c"
        src.write_text("static int parse(const char *p, unsigned long n)"
                       " { return n ? p[0] : 0; }\n", encoding="utf-8")
        got = replay_counterexample(HARNESS, src, "parse", [])
        assert got.status == "unchecked"
        assert "used zeroes" in got.detail

    def test_a_crash_on_no_inputs_is_still_a_crash(self, tmp_path):
        src = tmp_path / "x.c"
        src.write_text("static int parse(const char *p, unsigned long n)"
                       " { return p[8 + n]; }\n", encoding="utf-8")
        got = replay_counterexample(HARNESS, src, "parse", [])
        assert got.status == "crashed"

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

    def test_a_return_type_that_is_not_one_name_is_not_declared(self, tmp_path):
        """parson: a macro ending in `while (0)` sits before the function,
        and the return type read back was `while (0) int`."""
        from veripp.cppsig import Signature
        from veripp.harness import _emit_leak_roots

        sig = Signature(name="f", return_type="while (0) int", params=[])
        call = ["(void)f();"]
        _emit_leak_roots([], call, sig, {})
        assert call == ["(void)f();"]

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

    def test_a_leak_with_no_allocation_in_the_code_stays_an_artifact(self, tmp_path):
        """inih's test allocator: `return realloc(ptr, size)` on the
        harness's stack buffer leaks inside the checker's realloc model."""
        from veripp.esbmc import SourceLoc, ViolatedProperty, VerifyConfig, VerifyResult, Outcome
        from veripp.triage import mechanical_artifact

        harness = tmp_path / "veripp_harness_f.c"
        harness.write_text("static const void *veripp_kept_result;\n", encoding="utf-8")

        def result(allocated_at, where=harness):
            prop = ViolatedProperty(
                loc=SourceLoc(str(where), 39, 1, "main"),
                description="dereference failure: forgotten memory: dynamic_2_array",
                allocated_at=allocated_at)
            return VerifyResult(outcome=Outcome.COUNTEREXAMPLE, config=VerifyConfig(),
                                properties=[prop])

        assert mechanical_artifact(result(None), harness) is not None
        assert mechanical_artifact(
            result(SourceLoc("/src/heap.c", 22, 5, "dup_len")), harness) is None
        # C++ is not asked for the line: the trace never shows a `new`.
        cxx = tmp_path / "veripp_harness_f.cpp"
        plain = tmp_path / "plain.cpp"
        plain.write_text("int *make() { return new int(1); }\n", encoding="utf-8")
        cxx.write_text(f'#include "{plain}"\n// kept reachable (veripp_kept_*)\n',
                       encoding="utf-8")
        assert mechanical_artifact(result(None, cxx), cxx) is None
        # ...unless the unit reallocates: the checker's realloc model leaves
        # a block of its own behind when it reports failure, with no line.
        plain.write_text("void *grow(void *p) { return realloc(p, 8); }\n",
                         encoding="utf-8")
        assert mechanical_artifact(result(None, cxx), cxx) is not None

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


DOUBLE = """\
#include <stdlib.h>
int make_out(char **out) { if (!out) return -1; *out = malloc(4); return *out ? 0 : -1; }
int digit(const char **p) {
    int v = 0;
    if (**p >= '0' && **p <= '9') { v = **p - '0'; (*p)++; }
    return v;
}
void skip8(const char **p) { *p += 8; if (**p == 'x') (*p)++; }
"""


class TestPointersToPointers:
    """`char **out` and `const char **cursor` share a type and not a contract."""

    def test_an_out_parameter_starts_null(self, tmp_path):
        src = tmp_path / "dp.c"
        src.write_text(DOUBLE, encoding="utf-8")
        harness = generate(src, "make_out")
        assert "char * out_slot = 0;" in harness.code
        assert "char ** out = &out_slot;" in harness.code
        assert any("starts null" in a for a in harness.assumptions)

    def test_a_cursor_points_at_text(self, tmp_path):
        src = tmp_path / "dp.c"
        src.write_text(DOUBLE, encoding="utf-8")
        harness = generate(src, "digit")
        assert "const char * p_slot = p_buf;" in harness.code
        assert any("reads through it" in a for a in harness.assumptions)

    def test_a_slot_copied_into_a_local_is_read_through(self, tmp_path):
        """parson's parse_utf16: `char *out = *processed; *out++ = c;`."""
        src = tmp_path / "u.c"
        src.write_text("void put(char **processed, char c) {\n"
                       "    char *out = *processed;\n    *out = c;\n"
                       "    *processed = out;\n}\n", encoding="utf-8")
        harness = generate(src, "put")
        assert "char * processed_slot = processed_buf;" in harness.code

    @pytest.mark.esbmc
    def test_they_verify_and_the_overrun_does_not(self, tmp_path, capsys):
        from veripp.cli import main

        src = tmp_path / "dp.c"
        src.write_text(DOUBLE, encoding="utf-8")
        assert main(["verify", str(src), "--function", "make_out", "--no-llm"]) == 0
        assert main(["verify", str(src), "--function", "digit", "--no-llm"]) == 0
        assert main(["verify", str(src), "--function", "skip8", "--no-llm"]) == 1


HEAP_CPP = """\
#include <stdlib.h>
struct Buf { char *p; int n; };
class Stack {
public:
    Stack() : data_(0), size_(0) {}
    ~Stack() { free(data_); }
    void push(int v) {
        int *bigger = (int *)malloc((size_ + 1) * sizeof(int));
        if (!bigger) return;
        for (int i = 0; i < size_; i++) bigger[i] = data_[i];
        bigger[size_++] = v;
        data_ = bigger;
    }
    int size() const { return size_; }
private:
    int *data_;
    int size_;
};
void buf_init(Buf &b) { b.p = (char *)malloc(4); b.n = 4; }
char *make() { return new char[4]; }
int roundtrip(int v) { int *p = new int(v); int r = *p; delete p; return r; }
int lose(int v) { int *p = new int(v); return *p; }
"""


class TestLeaksInCxx:
    """Copying an object into a static can run a copy constructor, so C++
    was left out at first and every C++ leak stayed an artifact. A trivially
    copyable object is copied as C copies a struct; a class with its own
    copy semantics has its own destructor, which is what frees its memory."""

    def test_only_what_is_trivially_copyable_is_kept(self, tmp_path):
        src = tmp_path / "heap.cpp"
        src.write_text(HEAP_CPP, encoding="utf-8")
        code = generate(src, "buf_init").code
        assert "veripp_keeper<__is_trivially_copyable(__typeof__(x))>" in code
        assert "VERIPP_KEEP(1, b_obj);" in code

    def test_a_function_with_nothing_to_keep_still_says_it_looked(self, tmp_path):
        """`int lose(int)` holds no pointer; without the line its leak was
        still filed as the harness's own, in C as well."""
        src = tmp_path / "heap.cpp"
        src.write_text(HEAP_CPP, encoding="utf-8")
        code = generate(src, "lose").code
        assert "kept reachable for the leak check (veripp_kept_*)" in code
        assert "VERIPP_KEEP(" not in code.split("int main()")[1]

    @pytest.mark.esbmc
    def test_new_without_delete_is_found_and_the_rest_prove(self, tmp_path):
        from veripp.esbmc import VerifyConfig
        from veripp.harness import HarnessOptions
        from veripp.paths import contracts_include_dir
        from veripp.scan import scan

        src = tmp_path / "heap.cpp"
        src.write_text(HEAP_CPP, encoding="utf-8")
        config = VerifyConfig(timeout_s=120, include_dirs=[contracts_include_dir()])
        report = scan(src, config, HarnessOptions(), jobs=2, retry_budget=0)
        found = {r.name: r for r in report.counterexamples}
        assert set(found) == {"lose"}, report.summary()
        assert {"buf_init", "make", "roundtrip", "push", "size"} <= {
            r.name for r in report.proved}, report.summary()
        # The trace never shows a `new`, so it is filed at the function --
        # in the file, not in a harness that no longer exists.
        assert Path(found["lose"].file).name == "heap.cpp"
        assert found["lose"].line == 22

    @pytest.mark.esbmc
    def test_a_sequence_finds_the_block_push_never_frees(self, tmp_path, capsys):
        """One push on a fresh Stack leaks nothing. The second one does."""
        from veripp.cli import main

        src = tmp_path / "heap.cpp"
        src.write_text(HEAP_CPP, encoding="utf-8")
        assert main(["verify", str(src), "--class", "Stack", "--no-llm"]) == 1
        out = capsys.readouterr().out
        assert "forgotten memory" in out and "in push" in out

    @pytest.mark.esbmc
    def test_a_scalar_only_leak_in_c_is_found(self, tmp_path, capsys):
        from veripp.cli import main

        src = tmp_path / "l.c"
        src.write_text(
            "#include <stdlib.h>\n"
            "int lose(int v) { int *p = malloc(sizeof *p); if (!p) return 0;"
            " *p = v; return *p; }\n", encoding="utf-8")
        assert main(["verify", str(src), "--function", "lose", "--no-llm"]) == 1
        assert "allocated at" in capsys.readouterr().out
