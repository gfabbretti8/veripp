"""Sequence harnesses: drive a class through many calls, not just one.

A single call on a default-constructed object is a very weak question about a
stateful type. These tests pin that the generated sequence explores states a
single call cannot reach.
"""

import pytest

from veripp.cli import EXIT_COUNTEREXAMPLE, EXIT_VERIFIED, main
from veripp.cppsig import SignatureError, find_class
from veripp.harness import HarnessError, HarnessOptions, generate_sequence

SOURCE = """\
#include "veripp/contracts.hpp"

class Stack {
    int data_[4];
    unsigned size_ = 0;
    void slide() { }                 // private: must not be driven
public:
    static constexpr unsigned cap = 4;
    bool push(int v) {
        if (size_ == cap) return false;
        data_[size_++] = v;
        VERIPP_ENSURES(size_ <= cap);
        return true;
    }
    bool pop(int& out) {
        if (size_ == 0) return false;
        out = data_[--size_];
        return true;
    }
    unsigned size() const { return size_; }
private:
    int secret() const { return 7; }  // private again
};

struct Counter {
    int n = 0;
    void bump() { ++n; }
};
"""


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "s.cpp"
    p.write_text(SOURCE, encoding="utf-8")
    return p


class TestClassSurface:
    def test_only_public_methods_are_exposed(self):
        info = find_class(SOURCE, "Stack")
        assert {m.name for m in info.methods} == {"push", "pop", "size"}
        assert info.default_constructible

    def test_struct_members_default_to_public(self):
        info = find_class(SOURCE, "Counter")
        assert info.is_struct
        assert {m.name for m in info.methods} == {"bump"}

    def test_missing_class_is_refused(self):
        with pytest.raises(SignatureError, match="no definition of class"):
            find_class(SOURCE, "Nope")

    def test_class_template_is_refused(self):
        src = "template <class T> class Box { public: T get() { return T(); } };"
        with pytest.raises(SignatureError, match="class template"):
            find_class(src, "Box")


class TestSequenceHarness:
    def test_drives_every_public_method(self, src):
        code = generate_sequence(src, "Stack").code
        assert "Stack veripp_obj;" in code
        for method in ("push", "pop", "size"):
            assert f"veripp_obj.{method}(" in code
        assert "secret" not in code and "slide" not in code

    def test_choice_is_bounded_to_the_method_count(self, src):
        code = generate_sequence(src, "Stack").code
        assert "VERIPP_ASSUME(veripp_choice < 3);" in code

    def test_sequence_length_is_configurable_and_disclosed(self, src):
        harness = generate_sequence(src, "Stack", HarnessOptions(max_calls=7))
        assert "veripp_step < 7" in harness.code
        assert any("at most 7 calls" in a for a in harness.assumptions)

    def test_assertions_are_checked_after_every_call(self, src):
        code = generate_sequence(
            src, "Stack", assertions=["veripp_obj.size() <= Stack::cap"]
        ).code
        assert "VERIPP_ASSERT(veripp_obj.size() <= Stack::cap);" in code

    def test_undrivable_methods_are_disclosed_not_hidden(self, tmp_path):
        p = tmp_path / "s.cpp"
        p.write_text(
            '#include "veripp/contracts.hpp"\n'
            "#include <string>\n"
            "class C { public:\n"
            "  void ok(int v) { (void)v; }\n"
            "  void hard(std::string s) { (void)s; }\n"
            "};\n"
        , encoding="utf-8")
        harness = generate_sequence(p, "C")
        assert "veripp_obj.ok(" in harness.code
        assert "veripp_obj.hard(" not in harness.code
        assert any("hard" in a and "unexplored" in a for a in harness.assumptions)

    def test_class_with_no_drivable_method_is_refused(self, tmp_path):
        p = tmp_path / "s.cpp"
        p.write_text("#include <string>\nclass C { public: void f(std::string s) { (void)s; } };\n", encoding="utf-8")
        with pytest.raises(HarnessError, match="no public method"):
            generate_sequence(p, "C")


@pytest.mark.esbmc
class TestSequenceFindsStatefulBugs:
    """The point of the feature: states a single call cannot reach."""

    def _broken(self, tmp_path):
        p = tmp_path / "broken.cpp"
        # Capacity check removed: only an overflowing SEQUENCE reveals it.
        p.write_text(SOURCE.replace("if (size_ == cap) return false;", ""))
        return p

    def test_single_call_misses_the_overflow(self, capsys, tmp_path):
        code = main(["verify", str(self._broken(tmp_path)), "--function", "push",
                     "--no-llm", "--timeout", "120"])
        assert code == EXIT_VERIFIED  # correct: one push cannot overflow

    def test_sequence_catches_it(self, capsys, tmp_path):
        code = main(["verify", str(self._broken(tmp_path)), "--class", "Stack",
                     "--max-calls", "5", "--unwind", "8", "--no-llm", "--timeout", "300"])
        out = capsys.readouterr().out
        assert code == EXIT_COUNTEREXAMPLE
        # Overflowing the buffer breaks two things at once: the bounds of
        # data_ and the postcondition. A checker that has esbmc#6508 fixed
        # reports the bounds violation; one that does not still catches the
        # postcondition. Either proves the sequence reached a state a single
        # call cannot.
        assert "array bounds violated" in out or "size_ <= cap" in out
        assert "in push" in out

    def test_correct_class_verifies_over_all_sequences(self, capsys, src):
        code = main(["verify", str(src), "--class", "Stack", "--max-calls", "5",
                     "--unwind", "8", "--no-llm", "--timeout", "300",
                     "--assert", "veripp_obj.size() <= Stack::cap"])
        out = capsys.readouterr().out
        assert code == EXIT_VERIFIED
        assert "at most 5 calls" in out


class TestCHandleSequences:
    """Construct with the library's own constructor, drive, then free.

    A single call on an object the harness invented is both a weak question
    and a noisy one -- most of this project's false positives were struct
    graphs filled field by field, describing objects the library cannot
    build. An object the library built is well formed by construction, and a
    sequence of its own functions is where a stateful C API's bugs live.
    """

    SRC = (
        '#include "veripp/contracts.hpp"\n'
        "#include <stdlib.h>\n"
        "typedef struct list_t list_t;\n"
        "struct list_t { int count; int cap; };\n"
        "list_t *list_new(void) { return (list_t*)malloc(sizeof(list_t)); }\n"
        "list_t *list_new_big(void) { return (list_t*)malloc(sizeof(list_t)); }\n"
        "void list_free(list_t *l) { free(l); }\n"
        "int list_count(list_t *l) { return l->count; }\n"
        "void list_reserve(list_t *l, int n) { l->cap = n; }\n"
        "void list_merge(list_t *a, list_t *b) { a->count += b->count; }\n"
    )

    def _harness(self, tmp_path, src=None, **kw):
        from veripp.harness import generate_c_sequence

        p = tmp_path / "s.c"
        p.write_text(src if src is not None else self.SRC, encoding="utf-8")
        return generate_c_sequence(p, "list_t", HarnessOptions(max_calls=2), **kw)

    def test_every_constructor_is_offered(self, tmp_path):
        code = self._harness(tmp_path).code
        assert "list_new()" in code and "list_new_big()" in code
        assert "VERIPP_ASSUME(veripp_handle != 0);" in code

    def test_the_functions_are_driven_in_a_loop(self, tmp_path):
        code = self._harness(tmp_path).code
        assert "for (int veripp_step = 0; veripp_step < 2; ++veripp_step) {" in code
        assert "list_count(veripp_handle)" in code
        assert "list_reserve(veripp_handle, n)" in code

    def test_the_object_is_freed_once_at_the_end(self, tmp_path):
        code = self._harness(tmp_path).code
        assert code.count("list_free(veripp_handle);") == 1
        assert "list_free" not in code.split("for (int veripp_step")[1].split("}")[0]

    def test_the_deallocator_is_not_one_of_the_calls(self, tmp_path):
        """Freeing mid-sequence and then using the handle is a caller's
        error. Reporting it would say nothing about the library."""
        assumptions = self._harness(tmp_path).assumptions
        assert any("deliberately NOT one of the calls" in a for a in assumptions)

    def test_a_second_handle_parameter_is_refused(self, tmp_path):
        """`list_merge(a, b)` -- borrowed or handed over? The signature does
        not say, and either guess manufactures a finding."""
        harness = self._harness(tmp_path)
        assert "list_merge" not in harness.code.split("int main")[1]
        assert any("borrowed or handed over" in a for a in harness.assumptions)

    def test_the_call_set_can_be_narrowed(self, tmp_path):
        code = self._harness(tmp_path, only=["list_count"]).code
        assert "list_count(veripp_handle)" in code
        assert "list_reserve(veripp_handle" not in code

    def test_a_type_with_no_constructor_is_refused(self, tmp_path):
        from veripp.harness import HarnessError, generate_c_sequence

        src = (
            '#include "veripp/contracts.hpp"\n'
            "typedef struct box_t box_t;\n"
            "struct box_t { int n; };\n"
            "int box_get(box_t *b) { return b->n; }\n"
        )
        p = tmp_path / "s.c"
        p.write_text(src, encoding="utf-8")
        with pytest.raises(HarnessError, match="no constructor"):
            generate_c_sequence(p, "box_t")

    def test_the_bound_on_the_sequence_is_disclosed(self, tmp_path):
        assumptions = self._harness(tmp_path).assumptions
        assert any("Longer sequences are NOT explored" in a for a in assumptions)
