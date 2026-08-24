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
    p.write_text(SOURCE)
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
        )
        harness = generate_sequence(p, "C")
        assert "veripp_obj.ok(" in harness.code
        assert "veripp_obj.hard(" not in harness.code
        assert any("hard" in a and "unexplored" in a for a in harness.assumptions)

    def test_class_with_no_drivable_method_is_refused(self, tmp_path):
        p = tmp_path / "s.cpp"
        p.write_text("#include <string>\nclass C { public: void f(std::string s) { (void)s; } };\n")
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
        assert "size_ <= cap" in out  # the postcondition a sequence can break

    def test_correct_class_verifies_over_all_sequences(self, capsys, src):
        code = main(["verify", str(src), "--class", "Stack", "--max-calls", "5",
                     "--unwind", "8", "--no-llm", "--timeout", "300",
                     "--assert", "veripp_obj.size() <= Stack::cap"])
        out = capsys.readouterr().out
        assert code == EXIT_VERIFIED
        assert "at most 5 calls" in out
