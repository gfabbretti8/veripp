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
        src.write_text(SOURCE)
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
        src.write_text(SOURCE)
        code = generate(src, "sum_array").code
        assert "VERIPP_ASSUME(n > 0);" in code

    def test_preconditions_over_members_are_left_alone(self, tmp_path):
        """`count_ < limit` does not exist in main(); hoisting it would not compile."""
        src = tmp_path / "s.cpp"
        src.write_text(SOURCE)
        code = generate(src, "add").code
        assert "VERIPP_ASSUME(v >= 0);" in code
        assert "count_" not in code

    def test_member_call_uses_a_default_constructed_receiver(self, tmp_path):
        src = tmp_path / "s.cpp"
        src.write_text(SOURCE)
        harness = generate(src, "add")
        assert "Widget veripp_obj;" in harness.code
        assert "veripp_obj.add(v, weight)" in harness.code
        assert any("exactly one call" in a for a in harness.assumptions)

    def test_static_member_is_called_on_the_class(self, tmp_path):
        src = tmp_path / "s.cpp"
        src.write_text(SOURCE)
        code = generate(src, "clamp").code
        assert "(void)Widget::clamp(v);" in code
        assert "veripp_obj" not in code

    def test_void_return_is_not_cast(self, tmp_path):
        src = tmp_path / "s.cpp"
        src.write_text(SOURCE)
        code = generate(src, "fill").code
        assert "fill(out, out_len);" in code
        assert "(void)fill" not in code

    def test_assumptions_are_written_into_the_harness_header(self, tmp_path):
        src = tmp_path / "s.cpp"
        src.write_text(SOURCE)
        harness = generate(src, "sum_array")
        header = harness.code.split("int main()")[0]
        for assumption in harness.assumptions:
            assert assumption in header

    def test_unmodellable_parameter_is_refused(self, tmp_path):
        src = tmp_path / "s.cpp"
        src.write_text("#include <string>\nint f(std::string s) { return 0; }")
        with pytest.raises(HarnessError, match="cannot build a nondeterministic value"):
            generate(src, "f")

    def test_unguarded_main_is_refused(self, tmp_path):
        src = tmp_path / "s.cpp"
        src.write_text("int f(int x) { return x; }\nint main() { return f(1); }")
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
        )
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
        )
        assert "it_obj" in generate(p, "peek").code
