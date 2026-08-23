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
