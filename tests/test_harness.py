"""Harness generation: signature recovery, nondet inputs, hoisted preconditions."""

import pytest

from veripp.cppsig import SignatureError, find_function
from veripp.harness import HarnessError, HarnessOptions, generate, normalize_type

SOURCE = """\
#include "veripp/contracts.hpp"

int sum_array(const int* a, unsigned n) {
    VERIPP_REQUIRES(n > 0);
    int s = 0;
    for (unsigned i = 0; i < n; ++i) s += a[i];
    return s;
}

class Widget {
    int count_ = 0;
public:
    static constexpr int limit = 8;
    bool add(int v, double weight = 1.0) {
        VERIPP_REQUIRES(v >= 0);
        VERIPP_REQUIRES(count_ < limit);   // mentions a member: cannot be hoisted
        ++count_;
        return true;
    }
    static int clamp(int v) { return v < 0 ? 0 : v; }
    int count() const { return count_; }
};

void fill(char* out, unsigned long out_len) { (void)out; (void)out_len; }
"""


class TestSignature:
    def test_free_function(self):
        sig = find_function(SOURCE, "sum_array")
        assert sig.return_type == "int"
        assert sig.class_name is None
        assert [(p.type, p.name) for p in sig.params] == [
            ("const int*", "a"),
            ("unsigned", "n"),
        ]
        assert sig.params[0].is_pointer and sig.params[0].pointee() == "int"

    def test_member_function_with_default_argument(self):
        sig = find_function(SOURCE, "add")
        assert sig.class_name == "Widget"
        assert sig.qualified_name == "Widget::add"
        assert not sig.is_static
        assert [p.name for p in sig.params] == ["v", "weight"]
        assert sig.params[1].type == "double"  # the `= 1.0` is dropped

    def test_static_and_const_members(self):
        assert find_function(SOURCE, "clamp").is_static
        assert find_function(SOURCE, "count").is_const

    def test_declaration_without_definition_is_not_a_target(self):
        with pytest.raises(SignatureError, match="no definition"):
            find_function("int f(int x);\nint main() { return f(1); }", "f")

    def test_overloads_are_refused_rather_than_guessed(self):
        src = "int f(int x) { return x; }\nint f(long x) { return 0; }"
        with pytest.raises(SignatureError, match="defined 2 times"):
            find_function(src, "f")

    def test_comments_do_not_leak_into_the_return_type(self):
        src = '// a comment mentioning f\n#include "x.hpp"\nint f(int x) { return x; }'
        assert find_function(src, "f").return_type == "int"


class TestHarness:
    def test_buffer_paired_with_its_length(self, tmp_path):
        src = tmp_path / "s.cpp"
        src.write_text(SOURCE, encoding="utf-8")
        harness = generate(src, "sum_array", HarnessOptions(max_array_len=3))

        assert "unsigned n = VERIPP_NONDET_UINT();" in harness.code
        assert "VERIPP_ASSUME(n <= 3);" in harness.code
        assert "int a_buf[3];" in harness.code
        assert "const int* a = a_buf;" in harness.code
        assert "(void)sum_array(a, n);" in harness.code
        # The bound is a real restriction, so it has to be stated.
        assert any("harness bound on array length" in a for a in harness.assumptions)

    def test_preconditions_over_parameters_are_hoisted(self, tmp_path):
        src = tmp_path / "s.cpp"
        src.write_text(SOURCE, encoding="utf-8")
        code = generate(src, "sum_array").code
        assert "VERIPP_ASSUME(n > 0);" in code

    def test_preconditions_over_members_are_left_alone(self, tmp_path):
        """`count_ < limit` does not exist in main(); hoisting it would not compile."""
        src = tmp_path / "s.cpp"
        src.write_text(SOURCE, encoding="utf-8")
        code = generate(src, "add").code
        assert "VERIPP_ASSUME(v >= 0);" in code
        assert "count_" not in code

    def test_member_call_uses_a_default_constructed_receiver(self, tmp_path):
        src = tmp_path / "s.cpp"
        src.write_text(SOURCE, encoding="utf-8")
        harness = generate(src, "add")
        assert "Widget veripp_obj;" in harness.code
        assert "veripp_obj.add(v, weight)" in harness.code
        assert any("exactly one call" in a for a in harness.assumptions)

    def test_static_member_is_called_on_the_class(self, tmp_path):
        src = tmp_path / "s.cpp"
        src.write_text(SOURCE, encoding="utf-8")
        code = generate(src, "clamp").code
        assert "(void)Widget::clamp(v);" in code
        assert "veripp_obj" not in code

    def test_void_return_is_not_cast(self, tmp_path):
        src = tmp_path / "s.cpp"
        src.write_text(SOURCE, encoding="utf-8")
        code = generate(src, "fill").code
        assert "fill(out, out_len);" in code
        assert "(void)fill" not in code

    def test_assumptions_are_written_into_the_harness_header(self, tmp_path):
        src = tmp_path / "s.cpp"
        src.write_text(SOURCE, encoding="utf-8")
        harness = generate(src, "sum_array")
        header = harness.code.split("int main()")[0]
        for assumption in harness.assumptions:
            assert assumption in header

    def test_unmodellable_parameter_is_refused(self, tmp_path):
        src = tmp_path / "s.cpp"
        src.write_text("#include <string>\nint f(std::string s) { return 0; }", encoding="utf-8")
        with pytest.raises(HarnessError, match="cannot build a nondeterministic value"):
            generate(src, "f")

    def test_unguarded_main_is_refused(self, tmp_path):
        src = tmp_path / "s.cpp"
        src.write_text("int f(int x) { return x; }\nint main() { return f(1); }", encoding="utf-8")
        with pytest.raises(HarnessError, match="defines main\\(\\) unguarded"):
            generate(src, "f")

    def test_examples_guard_their_own_main(self, examples):
        for name in ("ring_buffer.cpp", "off_by_one.cpp"):
            harness = generate(examples / name, "push" if "ring" in name else "sum_array")
            assert "int main()" in harness.code


@pytest.mark.parametrize(
    "written,canonical",
    [
        ("unsigned int", "unsigned"),
        ("const int", "int"),
        ("std::size_t", "size_t"),
        ("unsigned long int", "unsigned long"),
        ("signed", "int"),
    ],
)
def test_type_normalisation(written, canonical):
    assert normalize_type(written) == canonical


class TestRefusalsFoundOnRealCode:
    """Shapes the scanner used to accept, found by running it over tinyxml2.

    Each of these produced a "signature" that could never compile, which is
    worse than refusing: the generator's whole contract is that it declines
    what it cannot model.
    """

    def test_destructor(self):
        with pytest.raises(SignatureError, match="destructor"):
            find_function("class C { public: ~C() { } };", "C")

    def test_member_of_a_class_template(self):
        src = "template <class T> class Box { T v_; public:\n  T get() { return v_; }\n};"
        with pytest.raises(SignatureError, match="class template"):
            find_function(src, "get")

    def test_function_template(self):
        with pytest.raises(SignatureError, match="function template"):
            find_function("template <typename T>\nT twice(T x) { return x + x; }", "twice")

    def test_last_entry_of_a_constructor_initialiser_list(self):
        """`: Base(0), a_(1), pool_()` followed by the body looked like a definition."""
        src = (
            "class D : public B {\n  int a_; Pool pool_;\npublic:\n"
            "  D(int a) :\n    B( 0 ),\n    a_( a ),\n    pool_()\n  {\n    a_ = 1;\n  }\n};"
        )
        with pytest.raises(SignatureError, match="initialiser list"):
            find_function(src, "pool_")

    def test_operator(self):
        with pytest.raises(SignatureError, match="operator"):
            find_function("struct C { bool operator(int x) { return true; } };", "operator")


class TestOutOfLineDefinitions:
    """`void C::Clear() { }` is the most common definition shape in real C++.

    All of these were refused with "could not determine the return type",
    because the head scan stopped at the `:` inside `::`.
    """

    def test_recovers_class_return_type_and_constness(self):
        src = (
            "class C { public: void Clear(); std::size_t N() const; };\n"
            "void C::Clear() { }\n"
            "std::size_t C::N() const { return 0; }\n"
        )
        clear = find_function(src, "Clear")
        assert (clear.return_type, clear.class_name, clear.qualified_name) == (
            "void", "C", "C::Clear",
        )
        n = find_function(src, "N")
        assert (n.return_type, n.class_name, n.is_const) == ("std::size_t", "C", True)

    def test_namespace_qualified_return_type_is_not_truncated(self):
        assert find_function("std::size_t f(int x) { return 0; }", "f").return_type == "std::size_t"

    def test_out_of_line_member_gets_a_receiver_in_the_harness(self, tmp_path):
        src = tmp_path / "s.cpp"
        src.write_text(
            '#include "veripp/contracts.hpp"\n'
            "class Counter { int n_; public:\n  Counter() : n_(0) {}\n  void bump(int by);\n};\n"
            "void Counter::bump(int by) {\n    VERIPP_REQUIRES(by >= 0);\n    n_ += by;\n}\n"
        , encoding="utf-8")
        code = generate(src, "bump").code
        assert "Counter veripp_obj;" in code
        assert "veripp_obj.bump(by);" in code
        assert "VERIPP_ASSUME(by >= 0);" in code

    def test_access_specifiers_and_labels_still_end_a_declaration(self):
        src = "class C {\npublic:\n  int f(int x) { return x; }\n};"
        assert find_function(src, "f").return_type == "int"


def test_two_preprocessor_directives_before_a_definition():
    """`_decl_head` advanced past each directive using offsets from the same
    slice, so a second `#include` compounded them and swallowed the return
    type. Real project files routinely have several."""
    src = (
        '#include "geom.h"\n'
        '#include "veripp/contracts.hpp"\n'
        "int area(Box* b) { return b->w * b->h; }\n"
    )
    sig = find_function(src, "area")
    assert sig.return_type == "int"
    assert [(p.type, p.name) for p in sig.params] == [("Box*", "b")]


class TestCIdioms:
    """Shapes that are ordinary in C and were refused outright.

    Both were found by scanning libraries veripp had never been tuned
    against; neither appears in lodepng, the only library it had been
    measured on.
    """

    def test_a_macro_wrapped_return_type_is_not_an_initialiser_list(self):
        """`CJSON_PUBLIC(cJSON *) f(...)` is how most C libraries mark exports.

        The parentheses made it look like a constructor initialiser list, and
        79 of cJSON's 117 functions were refused for being C++ syntax they
        could not possibly be.
        """
        src = "#define CJSON_PUBLIC(t) t\nCJSON_PUBLIC(int) add(int a, int b) { return a + b; }\n"
        sig = find_function(src, "add")
        assert [(p.type, p.name) for p in sig.params] == [("int", "a"), ("int", "b")]

    def test_a_real_initialiser_list_is_still_refused(self):
        src = (
            "class D : public B {\n  int a_;\npublic:\n"
            "  D(int a) :\n    B( 0 ),\n    a_( a )\n  {\n    a_ = 1;\n  }\n};"
        )
        with pytest.raises(SignatureError):
            find_function(src, "a_")

    def test_a_const_qualified_pointer_is_still_a_pointer(self):
        """`cJSON * const item` promises the pointer is not reassigned. That
        is nothing to a harness, but it hides the `*`, and a parameter not
        recognised as a pointer is refused."""
        src = "int size(const char * const s, unsigned n) { return (int)n; }"
        param = find_function(src, "size").params[0]
        assert param.is_pointer
        assert param.pointee() == "char"

    def test_const_pointer_parameters_can_be_harnessed(self, tmp_path):
        p = tmp_path / "s.c"
        p.write_text(
            '#include "veripp/contracts.hpp"\n'
            "typedef struct { int n; } item_t;\n"
            "static int peek(item_t * const it) { return it->n; }\n"
        , encoding="utf-8")
        assert "it_obj" in generate(p, "peek").code


class TestTextSlices:
    """A (buffer, length) pair whose body uses bounded str- routines on it.

    Left free, the solver puts a NUL inside the slice, the routine stops
    there, and the code is blamed for what it does with the "match". That
    manufactured two counterexamples on real code -- tinyexpr's find_builtin
    and parson's is_decimal -- and no tokeniser can produce either, because
    the length a caller passes describes the text it passes with it.

    The binary equivalents promise nothing of the sort, and the assumption
    must not fire there: parson's confirmed UTF-8 over-read is in a function
    that indexes its buffer directly, and assuming it away would have erased
    the only real finding in that file.
    """

    def _assumptions(self, tmp_path, body, params="const char *s, int n"):
        p = tmp_path / "s.c"
        p.write_text(
            '#include "veripp/contracts.hpp"\n#include <string.h>\n'
            f"int scan({params}) {{ {body} }}\n",
            encoding="utf-8",
        )
        return generate(p, "scan").assumptions

    def test_a_bounded_str_routine_makes_the_slice_text(self, tmp_path):
        assumptions = self._assumptions(tmp_path, 'return strncmp(s, "ab", n);')
        assert any("no terminator among them" in a for a in assumptions)

    def test_a_binary_routine_does_not(self, tmp_path):
        """memcmp says nothing about NULs, and neither may veripp."""
        assumptions = self._assumptions(tmp_path, 'return memcmp(s, "ab", n);')
        assert not any("no terminator among them" in a for a in assumptions)

    def test_plain_indexing_does_not(self, tmp_path):
        assumptions = self._assumptions(tmp_path, "return n > 0 ? s[0] : 0;")
        assert not any("no terminator among them" in a for a in assumptions)

    def test_the_assumption_cannot_itself_read_out_of_bounds(self, tmp_path):
        """A signed length is negative half the time, and casting it to
        size_t made the assumption loop overrun the buffer it was about."""
        p = tmp_path / "s.c"
        p.write_text(
            '#include "veripp/contracts.hpp"\n#include <string.h>\n'
            'int scan(const char *s, int n) { return strncmp(s, "ab", n); }\n',
            encoding="utf-8",
        )
        code = generate(p, "scan").code
        assert "VERIPP_ASSUME(n >= 0);" in code
        assert "veripp_i < 4UL &&" in code

    def test_a_negative_length_is_stated_not_assumed_silently(self, tmp_path):
        assumptions = self._assumptions(tmp_path, 'return strncmp(s, "ab", n);')
        assert any("0 <= n <= 4" in a for a in assumptions)

    def test_an_unsigned_length_needs_no_such_claim(self, tmp_path):
        p = tmp_path / "s.c"
        p.write_text(
            '#include "veripp/contracts.hpp"\n#include <string.h>\n'
            "int scan(const char *s, size_t n) "
            '{ return strncmp(s, "ab", n); }\n',
            encoding="utf-8",
        )
        harness = generate(p, "scan")
        assert "VERIPP_ASSUME(n >= 0);" not in harness.code
        assert any("with n <= 4" in a for a in harness.assumptions)


class TestRealWorldCPreprocessor:
    """Shapes found in zlib, the most deployed C library there is."""

    def test_a_main_behind_an_ifdef_does_not_disqualify_the_file(self, tmp_path):
        """zlib's crc32.c carries a table generator under `#ifdef MAKECRCH`.
        The harness never defines that macro, so the main is not compiled --
        but refusing on sight cost every function in the file."""
        p = tmp_path / "s.c"
        p.write_text(
            '#include "veripp/contracts.hpp"\n'
            "static int useful(int x) { return x + 1; }\n"
            "#ifdef MAKECRCH\n"
            "int main(void) { return 0; }\n"
            "#endif\n"
        , encoding="utf-8")
        assert "useful(x)" in generate(p, "useful").code

    def test_an_unguarded_main_is_still_refused(self, tmp_path):
        p = tmp_path / "s.c"
        p.write_text("int f(int x) { return x; }\nint main(void) { return f(1); }\n", encoding="utf-8")
        with pytest.raises(HarnessError, match="defines main"):
            generate(p, "f")

    def test_a_macro_that_expands_to_nothing_is_stripped_from_a_typedef(self):
        """zlib writes `typedef Byte FAR Bytef;` where FAR is `#define FAR`."""
        from veripp.cppsig import collect_scalar_typedefs

        src = (
            "#define FAR\n"
            "typedef unsigned char Byte;\n"
            "typedef Byte FAR Bytef;\n"
            "typedef unsigned long uLong;\n"
        )
        types = collect_scalar_typedefs(src)
        assert types["Bytef"] == "unsigned char"
        assert types["uLong"] == "unsigned long"

    def test_includes_are_followed_deep_enough_for_real_headers(self, tmp_path):
        """zlib reaches its typedefs through zutil.h -> zlib.h -> zconf.h."""
        from veripp.cppsig import collect_scalar_typedefs
        from veripp.harness import _with_local_includes

        (tmp_path / "level3.h").write_text("typedef unsigned long deep_t;\n", encoding="utf-8")
        (tmp_path / "level2.h").write_text('#include "level3.h"\n', encoding="utf-8")
        (tmp_path / "level1.h").write_text('#include "level2.h"\n', encoding="utf-8")
        src = tmp_path / "a.c"
        src.write_text('#include "level1.h"\nint f(deep_t x) { return (int)x; }\n', encoding="utf-8")
        expanded = _with_local_includes(src, src.read_text(encoding="utf-8"), [tmp_path])
        assert collect_scalar_typedefs(expanded).get("deep_t") == "unsigned long"


class TestTypeAliasChains:
    """zlib writes its whole public API through aliases veripp could not follow."""

    def test_a_pointer_typedef_makes_the_parameter_a_pointer(self, tmp_path):
        """`typedef z_stream FAR *z_streamp;` -- without following it, a
        `z_streamp` parameter is not seen as a pointer at all."""
        p = tmp_path / "s.c"
        p.write_text(
            '#include "veripp/contracts.hpp"\n'
            "#define FAR\n"
            "typedef struct z_stream_s { int avail; } z_stream;\n"
            "typedef z_stream FAR *z_streamp;\n"
            "static int avail(z_streamp s) { return s->avail; }\n"
        , encoding="utf-8")
        code = generate(p, "avail").code
        assert "z_stream s_obj;" in code
        assert "s_obj.avail = VERIPP_NONDET_INT();" in code

    def test_a_typedef_through_a_type_macro_resolves(self):
        """zlib reaches `unsigned long long` as z_word_t -> Z_U8 -> the type."""
        from veripp.cppsig import collect_scalar_typedefs

        src = "#define Z_U8 unsigned long long\ntypedef Z_U8 z_word_t;\n"
        assert collect_scalar_typedefs(src)["z_word_t"] == "unsigned long long"

    def test_a_function_like_macro_is_not_mistaken_for_a_type(self):
        from veripp.cppsig import collect_scalar_typedefs

        src = "#define MAX(a,b) ((a)>(b)?(a):(b))\ntypedef unsigned long uLong;\n"
        types = collect_scalar_typedefs(src)
        assert "MAX" not in types
        assert types["uLong"] == "unsigned long"


class TestFixedSizeWrites:
    """An output buffer with no length parameter is the worst case on real C.

    Its size lives in the caller's head -- except when it does not. MS-CHAP
    states it in the first line of the function that fills it:

        BZERO(response, MS_CHAP_RESPONSE_LEN);   /* 49 */

    Sizing `response` from the harness bound instead gave it 40 bytes and
    reported lwIP for the memset of 49. Three of chap_ms.c's four
    counterexamples were this one thing.
    """

    def _assumptions(self, tmp_path, src, fn="fill"):
        p = tmp_path / "s.c"
        p.write_text(src, encoding="utf-8")
        return generate(p, fn).assumptions

    def test_a_constant_size_sets_the_extent(self, tmp_path):
        src = (
            '#include "veripp/contracts.hpp"\n#include <string.h>\n'
            "void fill(unsigned char *out) { memset(out, 0, 49); }\n"
        )
        assert any("at least 49" in a for a in self._assumptions(tmp_path, src))

    def test_a_size_macro_is_resolved(self, tmp_path):
        src = (
            '#include "veripp/contracts.hpp"\n#include <string.h>\n'
            "#define RESP_LEN\t49\t/* Response length for MS-CHAP */\n"
            "void fill(unsigned char *out) { memset(out, 0, RESP_LEN); }\n"
        )
        assert any("at least 49" in a for a in self._assumptions(tmp_path, src))

    def test_a_two_argument_call_counts(self, tmp_path):
        """lwIP spells it BZERO(p, n), with no fill byte."""
        src = (
            '#include "veripp/contracts.hpp"\n#include <string.h>\n'
            "#define BZERO(p, n) memset((p), 0, (n))\n"
            "void fill(unsigned char *out) { BZERO(out, 49); }\n"
        )
        assert any("at least 49" in a for a in self._assumptions(tmp_path, src))

    def test_a_multiple_of_a_bounded_length(self, tmp_path):
        """`BZERO(unicode, ascii_len * 2)` -- two bytes per character."""
        src = (
            '#include "veripp/contracts.hpp"\n#include <string.h>\n'
            "void fill(const char *ascii, int ascii_len, unsigned char *wide)\n"
            "{ memset(wide, 0, ascii_len * 2); }\n"
        )
        assert any("at least 8" in a for a in self._assumptions(tmp_path, src))

    def test_the_reason_for_the_size_is_given(self, tmp_path):
        src = (
            '#include "veripp/contracts.hpp"\n#include <string.h>\n'
            "void fill(unsigned char *out) { memset(out, 0, 49); }\n"
        )
        assert any("fixed-size <string.h> call" in a
                   for a in self._assumptions(tmp_path, src))

    def test_a_variable_size_is_not_a_constant(self, tmp_path):
        """`memcpy(out, in, n)` with a free `n` says nothing about out."""
        src = (
            '#include "veripp/contracts.hpp"\n#include <string.h>\n'
            "void fill(unsigned char *out, int n) { memset(out, 0, n); }\n"
        )
        assert not any("fixed-size" in a for a in self._assumptions(tmp_path, src))


class TestTerminatorEvidenceAtAnyIndex:
    """`pointer[position] != '\\0'` is the same promise as `pointer[0]`.

    Requiring the literal `[0]` spelling meant cJSON_Utils' JSON Pointer
    walk was modelled as four unterminated bytes, so the loop ran off the end
    and decode_array_index_from_pointer was reported for an over-read it
    cannot commit. Reading the function that false positive nominated is how
    the real bug in it was found -- but the report itself was wrong, and a
    wrong report is a cost whether or not it happens to point somewhere.
    """

    def _is_string(self, tmp_path, body, param="const unsigned char *p"):
        src = (
            '#include "veripp/contracts.hpp"\n'
            f"int walk({param}) {{ {body} }}\n"
        )
        (tmp_path / "s.c").write_text(src, encoding="utf-8")
        return any("NUL-terminated string" in a
                   for a in generate(tmp_path / "s.c", "walk").assumptions)

    def test_a_variable_index_counts_as_terminator_evidence(self, tmp_path):
        assert self._is_string(
            tmp_path,
            "unsigned i = 0; while (p[i] != 0) { i++; } return (int)i;",
        )

    def test_the_literal_zero_index_still_counts(self, tmp_path):
        assert self._is_string(tmp_path, "return p[0] == 0 ? 1 : 0;")

    def test_an_expression_index_counts(self, tmp_path):
        assert self._is_string(
            tmp_path, "unsigned i = 0; return p[i + 1] != 0 ? 1 : 0;"
        )

    def test_a_body_that_tests_no_terminator_is_not_a_string(self, tmp_path):
        """An `unsigned char *` is binary data until the code says otherwise."""
        assert not self._is_string(tmp_path, "return p[0] + p[1];")


class TestCompiledOutCode:
    """A dead `#if` must not be reported as a type veripp cannot construct.

    lwIP's mppe.c came back 0 of 7 harnessable, every function refused for
    taking a `ppp_pcb *` -- a type veripp builds happily in five other files
    in the same tree. MPPE_SUPPORT was simply not enabled, so the file sat
    inside a dead #if and the preprocessed source contained neither the
    functions nor their types. Every message named the type system; none
    named the configuration.
    """

    SRC = (
        '#include "veripp/contracts.hpp"\n'
        "#if FEATURE_ON\n"
        "int scaled(int x) { return x * 2; }\n"
        "#endif\n"
        "int always(int x) { return x + 1; }\n"
    )

    def _generate(self, tmp_path, fn, **defines):
        from veripp.harness import HarnessOptions, generate

        p = tmp_path / "s.c"
        p.write_text(self.SRC, encoding="utf-8")
        return generate(p, fn, HarnessOptions(preprocess=True))

    def test_a_function_behind_a_dead_if_says_so(self, tmp_path):
        from veripp.harness import HarnessError

        with pytest.raises(HarnessError, match="not in this build"):
            self._generate(tmp_path, "scaled")

    def test_the_message_points_at_the_configuration(self, tmp_path):
        from veripp.harness import HarnessError

        with pytest.raises(HarnessError, match="#if"):
            self._generate(tmp_path, "scaled")

    def test_a_function_that_survives_is_harnessed(self, tmp_path):
        assert "always(x)" in self._generate(tmp_path, "always").code

    def test_the_check_is_skipped_without_preprocessing(self, tmp_path):
        """Without a preprocessor veripp reads the text, where both exist."""
        from veripp.harness import HarnessOptions, generate

        p = tmp_path / "s.c"
        p.write_text(self.SRC, encoding="utf-8")
        assert "scaled(x)" in generate(p, "scaled", HarnessOptions()).code
