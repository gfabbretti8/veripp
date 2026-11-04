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
    p.write_text(SOURCE, encoding="utf-8")
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
        , encoding="utf-8")
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
        p.write_text("int main(void) { return 0; }\nstatic int f(int x) { return x; }\n", encoding="utf-8")
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
        p.write_text(self.SRC, encoding="utf-8")
        config = VerifyConfig(
            timeout_s=90, unwind=2,
            include_dirs=[contracts_include_dir(), p.parent],
        )
        # retry_budget=0: this class tests the in-pass widening knob alone;
        # the retry pass would settle the bound either way (its own tests
        # live in test_scan_triage.py).
        return scan(p, config, HarnessOptions(), jobs=1,
                    escalations=escalations, retry_budget=0)

    def test_without_escalation_the_bound_is_hit(self, tmp_path):
        outcomes = {r.name: r.outcome for r in self._scan(tmp_path, 0).results}
        assert outcomes["walk"] == "unwind_limit"

    def test_with_escalation_it_settles(self, tmp_path):
        outcomes = {r.name: r.outcome for r in self._scan(tmp_path, 1).results}
        assert outcomes["walk"] != "unwind_limit"


class TestAdviceIsTimely:
    @pytest.mark.esbmc
    def test_no_llm_hint_on_a_proof(self, capsys, src, monkeypatch):
        """Telling someone their counterexamples will not be triaged is advice
        about a problem they do not have when the answer is 'verified'."""
        from veripp.cli import main

        for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "VERIPP_LLM_MODEL",
                    "VERIPP_LLM_BASE_URL"):
            monkeypatch.delenv(var, raising=False)
        main(["verify", str(src), "--function", "safe_add", "--timeout", "90"])
        assert "no LLM configured" not in capsys.readouterr().err

    @pytest.mark.esbmc
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
        p.write_text('#include "veripp/contracts.hpp"\nint f(int x) { return x; }\n', encoding="utf-8")
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

    text = (contracts_include_dir() / "veripp" / "contracts.hpp").read_text(encoding="utf-8")
    assert "#if defined(__cplusplus)" in text
    assert "_Bool" in text
    # the brace must be inside the guard, not produced by a macro
    assert 'extern "C" {' in text


class TestGeneratedCIsValidC:
    """A C harness must be valid C, not C++ that happens to compile.

    `static char x = nondet();` initialises static storage with a function
    call. C++ allows it; C does not. It broke 72 of cJSON's 117 functions,
    and never appeared in lodepng because lodepng is C++.
    """

    def test_no_static_local_carries_a_call_initialiser(self, tmp_path):
        from veripp.harness import generate

        p = tmp_path / "s.c"
        p.write_text(
            '#include "veripp/contracts.hpp"\n'
            "typedef struct node { struct node* next; char* label; int n; } node;\n"
            "static int depth(node* r) { return r->next ? r->n : 0; }\n"
        , encoding="utf-8")
        for line in generate(p, "depth").code.splitlines():
            stripped = line.strip()
            if stripped.startswith("static ") and "=" in stripped:
                raise AssertionError(f"static local with an initialiser: {stripped}")

    def test_pointer_targets_are_still_addressable(self, tmp_path):
        """Dropping `static` must not cost the pointee its address: every
        local lives in main(), which does not return before the call."""
        from veripp.harness import generate

        p = tmp_path / "s.c"
        p.write_text(
            '#include "veripp/contracts.hpp"\n'
            "typedef struct { int* count; } item;\n"
            "static int len(item* i) { return i->count ? 1 : 0; }\n"
        , encoding="utf-8")
        code = generate(p, "len").code
        assert "i_obj.count = &" in code

    def test_a_string_field_points_at_a_local_array(self, tmp_path):
        """A `char*` field is a C string, so its storage is an array rather
        than a single value -- which decays, and lives in main either way."""
        from veripp.harness import generate

        p = tmp_path / "s.c"
        p.write_text(
            '#include "veripp/contracts.hpp"\n'
            "typedef struct { char* label; } item;\n"
            "static int len(item* i) { return i->label ? 1 : 0; }\n"
        , encoding="utf-8")
        code = generate(p, "len").code
        assert "char i_obj_label_target[5];" in code
        assert "i_obj.label = i_obj_label_target;" in code


class TestEmptyScan:
    """"0 functions" is a useless answer when the fix is one flag away."""

    def test_an_empty_scan_does_not_crash(self, tmp_path):
        """summary() divided by the candidate count, so a file with no
        functions produced a traceback rather than a sentence."""
        from veripp.esbmc import VerifyConfig
        from veripp.harness import HarnessOptions
        from veripp.scan import scan

        p = tmp_path / "decls.h"
        p.write_text("int f(int);\nint g(void);\n", encoding="utf-8")
        report = scan(p, VerifyConfig(), HarnessOptions(), jobs=1)
        assert report.candidates == 0
        assert "no function definitions found" in report.summary()

    def test_a_single_header_wrapper_is_pointed_at_its_header(self, tmp_path):
        from veripp.esbmc import VerifyConfig
        from veripp.harness import HarnessOptions
        from veripp.scan import scan

        (tmp_path / "xxhash.h").write_text("int hash(int x) { return x; }\n", encoding="utf-8")
        wrapper = tmp_path / "xxhash.c"
        wrapper.write_text("#define XXH_IMPLEMENTATION\n#include \"xxhash.h\"\n", encoding="utf-8")
        summary = scan(wrapper, VerifyConfig(), HarnessOptions(), jobs=1).summary()
        assert "The code is in the header" in summary
        assert "xxhash.h" in summary
        assert "-D XXH_IMPLEMENTATION" in summary

    def test_a_plain_header_gets_the_generic_advice(self, tmp_path):
        from veripp.esbmc import VerifyConfig
        from veripp.harness import HarnessOptions
        from veripp.scan import scan

        p = tmp_path / "api.h"
        p.write_text("int f(int);\n", encoding="utf-8")
        assert "scan the .c/.cpp" in scan(p, VerifyConfig(), HarnessOptions(), jobs=1).summary()


class TestUnusedLengthParameters:
    """A length-shaped parameter the body never reads.

    The most productive single shape in this project's hunting, and the only
    one that needs no solver. lwIP's netbiosns_name_decode takes a
    name_dec_len, discards it with LWIP_UNUSED_ARG, and walks an inbound UDP
    datagram with no bound -- the worst finding in the report, and this check
    names it from the signature alone.

    It reports and does not judge: smtp_base64_encode has the same shape and
    is safe at every call site.
    """

    def _check(self, src):
        from veripp.cppsig import find_function
        from veripp.harness import unused_length_parameters

        return unused_length_parameters(find_function(src, "f"))

    def test_a_discarded_length_is_flagged(self):
        src = ("#define LWIP_UNUSED_ARG(x) (void)x\n"
               "int f(const char *p, int n) { LWIP_UNUSED_ARG(n); return *p; }\n")
        assert self._check(src) == (["n"], [])

    def test_a_void_cast_discard_counts(self):
        src = "int f(const char *p, int len) { (void)len; return *p; }\n"
        assert self._check(src) == (["len"], [])

    def test_a_length_used_only_in_an_assertion_is_separate(self):
        """A release build removes the assertion and with it the bound."""
        src = ("#define LWIP_ASSERT(m, x) do { } while(0)\n"
               "int f(char *out, int out_len) {\n"
               "  LWIP_ASSERT(\"too short\", out_len >= 4);\n"
               "  out[0] = 1; return 0; }\n")
        assert self._check(src) == ([], ["out_len"])

    def test_a_length_that_is_used_is_not_flagged(self):
        src = "int f(const char *p, int n) { return n > 0 ? p[n - 1] : 0; }\n"
        assert self._check(src) == ([], [])

    def test_a_non_length_parameter_is_ignored(self):
        """An unused `flags` is ordinary; an unused `len` is a red flag."""
        src = "int f(const char *p, int flags) { return *p; }\n"
        assert self._check(src) == ([], [])

    def test_a_pointer_is_not_a_length(self):
        src = "int f(char *buf, int *count) { return buf[0]; }\n"
        assert self._check(src) == ([], [])

    def test_a_length_whose_buffer_is_also_unused_is_not_a_smell(self):
        """lwIP's ccp_datainput discards `pkt` and `len` together: it reacts
        to a decompression error and never looks at the payload. A function
        not doing a job is not a function doing it wrongly."""
        src = ("#define LWIP_UNUSED_ARG(x) (void)x\n"
               "int f(const char *pkt, int len) {\n"
               "  LWIP_UNUSED_ARG(pkt); LWIP_UNUSED_ARG(len); return 1; }\n")
        assert self._check(src) == ([], [])

    def test_a_length_with_no_buffer_to_bound_is_not_a_smell(self):
        """The tcp_sent callbacks take a `len` and no buffer at all -- the
        signature is lwIP's, not the implementation's."""
        src = ("#define LWIP_UNUSED_ARG(x) (void)x\n"
               "int f(void *arg, int len) { LWIP_UNUSED_ARG(len); return 1; }\n")
        assert self._check(src) == ([], [])

    def test_the_buffer_being_used_is_what_makes_it_one(self):
        """netbiosns_name_decode writes through name_dec and ignores the
        length that bounds it. That is the shape."""
        src = ("#define LWIP_UNUSED_ARG(x) (void)x\n"
               "int f(char *name_dec, int name_dec_len) {\n"
               "  LWIP_UNUSED_ARG(name_dec_len);\n"
               "  name_dec[0] = 1; return 0; }\n")
        assert self._check(src) == (["name_dec_len"], [])

    def test_the_scan_reports_it(self, tmp_path):
        from veripp.scan import _unused_length_report

        src = ("#define LWIP_UNUSED_ARG(x) (void)x\n"
               "int f(const char *p, int n) { LWIP_UNUSED_ARG(n); return *p; }\n")
        assert _unused_length_report(src, ["f"]) == {"f": (["n"], [])}


class TestRetryKeepsDerivedFieldsInSync:
    """A second attempt replaces the property, so everything read off it must
    be recomputed.

    `access` was added to FunctionResult and populated on the first pass but
    not in the retry, so a function that settled on a harder attempt kept the
    label from a property it no longer had -- and reported a read as a write.
    That is the single thing that field must never do, since the whole point
    of it is to say which counterexamples are worth reading first.
    """

    def test_every_property_derived_field_is_updated_on_retry(self):
        """Whatever the retry sets from `prop`, it must set all of it."""
        import inspect

        from veripp import scan as scan_mod

        source = inspect.getsource(scan_mod._retry_pass)
        block = source[source.index("if settled:"):]
        for field in ("r.detail", "r.file", "r.line", "r.column", "r.cwes",
                      "r.access", "r.artifact", "r.harness_null"):
            assert field in block, (
                f"{field} is derived from the counterexample but the retry "
                "path does not refresh it; a settled retry would keep a "
                "value describing a property it no longer has"
            )


class TestWhoCanReachIt:
    """The first question a triager asks of a counterexample is who can get
    there. An external function is an entry point for every other
    translation unit; a static helper is reached only through the call sites
    in this one, each of which may guard the input the solver found. scan
    now says which, counts the callers it can see, and lists entry points
    first."""

    def _result(self, name, **kw):
        from veripp.scan import FunctionResult

        return FunctionResult(name=name, outcome="counterexample", detail="d", **kw)

    def test_external_functions_are_listed_before_static_helpers(self):
        from veripp.scan import ScanReport

        report = ScanReport(source=Path("f.c"), candidates=3)
        report.results = [
            self._result("helper", linkage="static", callers=2, access="write"),
            self._result("api_fn", linkage="external", callers=0, access="read"),
            self._result("other_helper", linkage="static", callers=0),
        ]
        summary = report.summary()
        listed = [line.strip().split(":")[0] for line in summary.splitlines()
                  if line.startswith("    ") and ":" in line and "d  [" in line]
        assert listed[0] == "api_fn", summary
        assert "api_fn: d  [read; external, no callers found in this unit]" in summary
        assert "helper: d  [write; static, 2 callers here]" in summary
        assert "other_helper: d  [static, no callers found here]" in summary

    def test_triage_still_outranks_linkage(self):
        """A model's label ranks, and only ranks -- but it ranks above who
        can reach the function, since a real_bug verdict on a static helper
        came from reading its callers."""
        from veripp.scan import ScanReport

        report = ScanReport(source=Path("f.c"), candidates=2)
        report.results = [
            self._result("api_fn", linkage="external", callers=3, triage_kind="harness_issue"),
            self._result("helper", linkage="static", callers=1, triage_kind="real_bug"),
        ]
        summary = report.summary()
        assert summary.index("helper: d") < summary.index("api_fn: d")

    def test_callers_are_counted_in_the_unit_and_in_linked_sources(self, tmp_path):
        from veripp.cppsig import scrub
        from veripp.scan import _caller_count

        # Call sites are lines: two calls on one line are one site.
        text = ("static int helper(int x) { return x; }\n"
                "int api(int y) { int a = helper(y) + helper(y);\n"
                "                 return a + helper(1); }\n"
                "int helper_two(int z) { return z; }\n"   # a different name
                "/* helper(0) in a comment does not count */\n")
        linked = ["int elsewhere(void) { return helper(5); }\n"]
        assert _caller_count("helper", text, scrub(text), []) == 2
        assert _caller_count("helper", text, scrub(text), linked) == 3
        assert _caller_count("api", text, scrub(text), linked) == 0

    def test_the_count_is_not_capped_at_what_a_prompt_is_shown(self):
        from veripp.cppsig import scrub
        from veripp.scan import _caller_count
        from veripp.triage import MAX_CALL_SITES

        calls = "\n".join(f"int c{i}(void) {{ return helper({i}); }}" for i in range(MAX_CALL_SITES + 5))
        text = "static int helper(int x) { return x; }\n" + calls + "\n"
        assert _caller_count("helper", text, scrub(text), []) == MAX_CALL_SITES + 5

    def test_the_json_report_carries_both(self, tmp_path, monkeypatch, capsys):
        import json

        from veripp import cli
        from veripp.esbmc import Outcome, VerifyConfig, VerifyResult

        monkeypatch.setattr("veripp.scan.run",
                            lambda path, config, esbmc_bin=None:
                            VerifyResult(Outcome.COUNTEREXAMPLE, config))
        src = tmp_path / "f.c"
        src.write_text("static int helper(int x) { return x; }\n"
                       "int api(int y) { return helper(y); }\n", encoding="utf-8")
        cli.main(["scan", str(src), "--json", "--no-llm", "--no-cache", "--retry-budget", "0"])
        report = json.loads(capsys.readouterr().out)
        by_name = {c["function"]: c for c in report["counterexamples"]}
        assert by_name["helper"]["linkage"] == "static" and by_name["helper"]["callers"] == 1
        assert by_name["api"]["linkage"] == "external" and by_name["api"]["callers"] == 0

    @pytest.mark.esbmc
    def test_end_to_end_on_a_real_scan(self, tmp_path):
        src = tmp_path / "reach.c"
        src.write_text(
            "static int helper(int d) { return 10 / d; }\n"
            "int api(int d) { return 10 / d; }\n"
            "int uses_helper(int d) { return d == 0 ? 0 : helper(d); }\n",
            encoding="utf-8",
        )
        config = VerifyConfig(timeout_s=90, include_dirs=[contracts_include_dir()])
        report = scan(src, config, HarnessOptions(), jobs=2, retry_budget=0)
        found = {r.name: r for r in report.counterexamples}
        assert {"helper", "api"} <= set(found), report.summary()
        assert found["api"].linkage == "external" and found["api"].callers == 0
        assert found["helper"].linkage == "static" and found["helper"].callers == 1
        summary = report.summary()
        assert summary.index("api: ") < summary.index("helper: ")
