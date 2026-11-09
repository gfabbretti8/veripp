"""The functions a first scan most needs to reach, and used to lose.

Pointing veripp at inih -- three hundred lines, one of the most-copied C
libraries there is -- harnessed 9 of its 42 functions. The public API takes a
callback and was refused for it; every example and test file has a main() and
was refused whole; and one of the two counterexamples was the harness
comparing two pointers it had put in different objects. These are the shapes
behind those three, each small enough to read.
"""

from __future__ import annotations

import pytest

from veripp.cppsig import find_function
from veripp.harness import HarnessError, generate

CALLBACKS = """\
#include <stddef.h>
typedef int (*visit_fn)(void *ctx, int value);
typedef char *(*read_fn)(char *buf, int n);
typedef void (*done_fn)(void);

int each(const int *a, size_t n, visit_fn fn, void *ctx) {
    for (size_t i = 0; i <= n; i++)      /* BUG: <= */
        if (fn(ctx, a[i])) return 1;
    return 0;
}
int each_ok(const int *a, size_t n, int (*fn)(void *, int), void *ctx) {
    for (size_t i = 0; i < n; i++)
        if (fn(ctx, a[i])) return 1;
    return 0;
}
int pump(read_fn next, done_fn done) {
    char line[8];
    int lines = 0;
    while (next(line, 8)) lines++;
    done();
    return lines;
}
"""


class TestCallbackParameters:
    def test_an_in_place_callback_keeps_its_name(self):
        sig = find_function(CALLBACKS, "each_ok")
        fn = sig.params[2]
        assert fn.name == "fn"
        assert fn.type == "int (*)(void *, int)"
        assert fn.function_pointer == ("int", "void *, int")
        assert not fn.is_pointer

    def test_a_typedefd_callback_gets_a_stub_of_its_signature(self, tmp_path):
        src = tmp_path / "cb.c"
        src.write_text(CALLBACKS, encoding="utf-8")
        harness = generate(src, "each")
        assert ("static int veripp_stub_param_fn(void *ctx, int value) "
                "{ return VERIPP_NONDET_INT(); }") in harness.code
        assert "visit_fn fn = veripp_stub_param_fn;" in harness.code
        said = next(a for a in harness.assumptions if a.startswith("`fn`"))
        assert "returns any value" in said
        assert "NOT modelled" in said

    def test_unnamed_stub_parameters_are_named(self, tmp_path):
        """A C definition before C23 may not leave them out."""
        src = tmp_path / "cb.c"
        src.write_text(CALLBACKS, encoding="utf-8")
        code = generate(src, "each_ok").code
        assert "veripp_stub_param_fn(void * veripp_a0, int veripp_a1)" in code
        assert "int (*fn)(void *, int) = veripp_stub_param_fn;" in code

    def test_a_pointer_returning_stub_says_it_only_returns_null(self, tmp_path):
        """Otherwise a proof of the NULL path reads as a proof of the loop."""
        src = tmp_path / "cb.c"
        src.write_text(CALLBACKS, encoding="utf-8")
        harness = generate(src, "pump")
        said = next(a for a in harness.assumptions if a.startswith("`next`"))
        assert "ALWAYS returns NULL" in said
        assert "covers the NULL path only" in said
        done = next(a for a in harness.assumptions if a.startswith("`done`"))
        assert "returns any value" not in done

    def test_a_typedef_written_two_ways_is_settled_by_the_preprocessor(self, tmp_path):
        """inih's ini_handler: with or without a trailing `int lineno`."""
        src = tmp_path / "two.c"
        src.write_text(
            "#if WITH_LINE\n"
            "typedef int (*handler)(void *user, int lineno);\n"
            "#else\n"
            "typedef int (*handler)(void *user);\n"
            "#endif\n"
            "int run(handler h, void *user) { return h(user); }\n",
            encoding="utf-8",
        )
        try:
            code = generate(src, "run").code
        except HarnessError as exc:  # no compiler on this machine
            assert "more than one way under #if" in str(exc)
            return
        assert "veripp_stub_param_h(void *user)" in code

    def test_a_struct_by_value_return_is_still_refused(self, tmp_path):
        src = tmp_path / "s.c"
        src.write_text(
            "struct pair { int a, b; };\n"
            "typedef struct pair (*make_fn)(void);\n"
            "int use(make_fn make) { return make().a; }\n",
            encoding="utf-8",
        )
        with pytest.raises(HarnessError):
            generate(src, "use")


RANGES = """\
const char *skip_digits(const char *p, const char *end) {
    while (p < end && *p >= '0' && *p <= '9') p++;
    return p;
}
int parse_u8(const char *p, const char *end) {
    int v = 0;
    while (*p >= '0' && *p <= '9' && p < end) {  /* BUG: reads before the test */
        v = (v * 10 + (*p - '0')) & 0xff; p++;
    }
    return v;
}
int same(const char *a, const char *end) { return *a == *end; }
long span(const int *first, const int *last) { return last - first; }
"""


class TestStartAndEndPointers:
    def test_two_pointers_the_body_compares_share_one_buffer(self, tmp_path):
        src = tmp_path / "r.c"
        src.write_text(RANGES, encoding="utf-8")
        harness = generate(src, "skip_digits")
        assert "const char * p = p_buf;" in harness.code
        assert "const char * end = p_buf + end_offset;" in harness.code
        assert "VERIPP_ASSUME(end_offset <= 4);" in harness.code
        assert any("point into one buffer" in a for a in harness.assumptions)

    def test_subtraction_is_evidence_too(self, tmp_path):
        src = tmp_path / "r.c"
        src.write_text(RANGES, encoding="utf-8")
        assert "last = first_buf + last_offset;" in generate(src, "span").code

    def test_a_name_alone_is_not_evidence(self, tmp_path):
        """`end` that is never compared with `a` is just another pointer."""
        src = tmp_path / "r.c"
        src.write_text(RANGES, encoding="utf-8")
        harness = generate(src, "same")
        assert "end_offset" not in harness.code
        assert not any("point into one buffer" in a for a in harness.assumptions)

    @pytest.mark.esbmc
    def test_the_good_one_proves_and_the_bad_one_does_not(self, tmp_path, capsys):
        from veripp.cli import main

        src = tmp_path / "r.c"
        src.write_text(RANGES, encoding="utf-8")
        assert main(["verify", str(src), "--function", "skip_digits", "--no-llm"]) == 0
        capsys.readouterr()
        assert main(["verify", str(src), "--function", "parse_u8", "--no-llm"]) == 1
        out = capsys.readouterr().out
        assert "array bounds violated" in out
        assert "Relational comparison" not in out


PROGRAM = """\
#include <stdio.h>
static int clamp(int v, int hi) { return v > hi ? hi : v; }
int main(int argc, char **argv) { printf("%d\\n", clamp(argc, 3)); return 0; }
"""


class TestASourceWithItsOwnMain:
    def test_its_main_is_set_aside_not_refused(self, tmp_path):
        src = tmp_path / "tool.c"
        src.write_text(PROGRAM, encoding="utf-8")
        code = generate(src, "clamp").code
        include = f'#include "{src.resolve()}"\n'
        assert f"#define main veripp_original_main\n{include}#undef main\n" in code
        assert code.count("int main() {") == 1

    def test_a_file_without_one_is_left_alone(self, tmp_path):
        src = tmp_path / "lib.c"
        src.write_text("int twice(int v) { return v / 2; }\n", encoding="utf-8")
        assert "veripp_original_main" not in generate(src, "twice").code

    @pytest.mark.esbmc
    def test_it_verifies(self, tmp_path, capsys):
        from veripp.cli import main

        src = tmp_path / "tool.c"
        src.write_text(PROGRAM, encoding="utf-8")
        assert main(["verify", str(src), "--function", "clamp", "--no-llm"]) == 0


@pytest.mark.esbmc
class TestTheReportShowsTheLine:
    def test_a_counterexample_quotes_the_source_and_claims_no_diagnosis(
        self, tmp_path, capsys, monkeypatch
    ):
        from veripp.cli import main

        src = tmp_path / "cb.c"
        src.write_text(CALLBACKS, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert main(["verify", "cb.c", "--function", "each", "--no-llm"]) == 1
        out = capsys.readouterr().out
        assert "  at cb.c:8:" in out                      # relative, not absolute
        assert "> 8 |         if (fn(ctx, a[i])) return 1;" in out
        assert "^" in out
        assert "Not triaged:" in out
        assert "real_bug" not in out
