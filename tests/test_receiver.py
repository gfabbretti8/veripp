"""The object a member function is called on.

A harness for `T::f` used to write `T veripp_obj;` whatever `T` was, and
leave the frontend to reject it. On tinyxml2 that was 57 of 91 harnesses,
each reported as a parse error in veripp's own output.
"""
from __future__ import annotations

import pytest

from veripp.cli import EXIT_COUNTEREXAMPLE, EXIT_VERIFIED, main
from veripp.cppsig import class_shape, find_class, find_class_ranges, scrub
from veripp.harness import HarnessError, generate, generate_sequence
from veripp.scan import _reason

SOURCE = """\
#define LIB_API
class LIB_API Node {
public:
    virtual ~Node() {}
    virtual int kind() const = 0;
    virtual int depth() const = 0;
    int twice(int v);
};
int Node::twice(int v) { return v / 2; }

class LIB_API Leaf : public Node {
public:
    int kind() const { return 1; }
    int half(int v);
};
int Leaf::half(int v) { return v / 2; }

class Twig final : public Leaf {
public:
    int depth() const { return 2; }
    int third(int v);
};
int Twig::third(int v) { return v / 3; }

class Owned {
public:
    int get(int v);
    static int make(int v);
private:
    Owned() {}
    ~Owned() {}
    int hidden(int v);
};
int Owned::get(int v) { return v; }
int Owned::make(int v) { return v; }
int Owned::hidden(int v) { return v; }

class Sealed {
public:
    int get(int v);
protected:
    Sealed(int) {}
private:
    Sealed(const Sealed&) = delete;
};
int Sealed::get(int v) { return v; }

class Ring {
public:
    explicit Ring(unsigned cap) : cap_(cap % 4) {}
    Ring(const Ring& other) : cap_(other.cap_) {}
    int at(unsigned cap, unsigned i);
private:
    unsigned cap_;
    int data_[4];
};
int Ring::at(unsigned cap, unsigned i) { (void)cap; return data_[i % 4] = 0; }

class Doc {
public:
    Doc(bool entities = true, int mode = 0) : mode_(mode) { (void)entities; }
    int mode(int v);
private:
    int mode_;
};
int Doc::mode(int v) { return mode_ + (v % 2); }

class Plain {
public:
    ~Plain() {}
    int id(int v);
};
int Plain::id(int v) { return v; }
"""


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "r.cpp"
    p.write_text(SOURCE, encoding="utf-8")
    return p


class TestClassShape:
    def test_a_class_behind_an_export_macro_is_found(self):
        names = {c.name for c in find_class_ranges(scrub(SOURCE))}
        assert {"Node", "Leaf", "Twig"} <= names

    def test_a_class_behind_an_attribute_is_found(self):
        text = 'class __attribute__((visibility("default"))) A { int x; };'
        assert [c.name for c in find_class_ranges(scrub(text))] == ["A"]

    def test_own_pure_virtual(self):
        assert class_shape(SOURCE, "Node").pure_virtual == "kind"

    def test_inherited_pure_virtual_not_overridden(self):
        assert class_shape(SOURCE, "Leaf").pure_virtual == "depth"

    def test_every_pure_virtual_overridden_along_the_chain(self):
        assert class_shape(SOURCE, "Twig").pure_virtual is None

    def test_a_destructor_is_not_a_constructor(self):
        info = class_shape(SOURCE, "Plain")
        assert info.default_constructible and not info.constructors
        assert info.destructor_access is None
        assert class_shape(SOURCE, "Owned").destructor_access == "private"

    def test_all_default_arguments_make_a_default_constructor(self):
        assert class_shape(SOURCE, "Doc").default_constructible

    def test_hidden_and_deleted_constructors(self):
        info = class_shape(SOURCE, "Sealed")
        assert not info.default_constructible and not info.constructors
        assert sorted(info.hidden_constructors) == ["deleted", "protected"]

    def test_member_access(self):
        access = class_shape(SOURCE, "Owned").member_access
        assert access["hidden"] == {"private"} and access["get"] == {"public"}

    def test_find_class_still_lists_only_public_methods(self):
        info = find_class(SOURCE, "Owned")
        # `get` is public too, and skipped: `Sealed` defines one as well.
        assert {m.name for m in info.methods} | set(info.skipped) == {"get", "make"}


class TestRefusals:
    @pytest.mark.parametrize("function, says, label", [
        ("twice", "`Node` is an abstract class (`kind` is pure virtual)",
         "member of an abstract class"),
        ("half", "`Leaf` is an abstract class (`depth` is pure virtual)",
         "member of an abstract class"),
        ("hidden", "`Owned::hidden` is a private member function",
         "private or protected member function"),
    ])
    def test_reason(self, src, function, says, label):
        with pytest.raises(HarnessError) as exc:
            generate(src, function)
        assert says in str(exc.value)
        assert _reason(str(exc.value)) == label

    def test_private_destructor(self, tmp_path):
        p = tmp_path / "o.cpp"
        p.write_text(SOURCE.replace("int Sealed::get(int v) { return v; }", ""),
                     encoding="utf-8")
        with pytest.raises(HarnessError, match="`Owned` has a private destructor") as exc:
            generate(p, "get")
        assert _reason(str(exc.value)) == "member of a class only the library can construct"

    def test_no_public_constructor(self, tmp_path):
        p = tmp_path / "s.cpp"
        p.write_text(SOURCE.replace("int Owned::get(int v) { return v; }", ""),
                     encoding="utf-8")
        with pytest.raises(HarnessError, match="are deleted and protected") as exc:
            generate(p, "get")
        assert "`Sealed` has no public constructor" in str(exc.value)
        assert _reason(str(exc.value)) == "member of a class only the library can construct"

    def test_a_static_member_needs_no_object(self, src):
        assert "Owned::make(v);" in generate(src, "make").code

    def test_sequence_on_an_abstract_class_is_refused(self, src):
        with pytest.raises(HarnessError, match="abstract class"):
            generate_sequence(src, "Node")

    def test_class_defined_elsewhere_is_harnessed_as_before(self, tmp_path):
        p = tmp_path / "e.cpp"
        p.write_text("int Far::f(int v) { return v; }\n", encoding="utf-8")
        assert "Far veripp_obj;" in generate(p, "f").code


class TestConstruction:
    def test_default_arguments_need_no_arguments(self, src):
        assert "Doc veripp_obj;" in generate(src, "mode").code

    def test_a_public_constructor_is_called(self, src):
        harness = generate(src, "at")
        # The method has a `cap` of its own; the constructor's is renamed,
        # and the copy constructor is not what builds the first object.
        assert "unsigned cap_ctor = VERIPP_NONDET_UINT();" in harness.code
        assert "Ring veripp_obj(cap_ctor);" in harness.code
        assert "veripp_obj.at(cap, i);" in harness.code
        assert any("straight out of its constructor, `Ring veripp_obj(cap_ctor)`" in a
                   for a in harness.assumptions)

    def test_concrete_derived_class_is_declared(self, src):
        assert "Twig veripp_obj;" in generate(src, "third").code


@pytest.mark.esbmc
class TestVerdicts:
    def test_constructor_built_receiver_verifies(self, src, capsys):
        assert main(["verify", str(src), "--function", "at", "--no-llm"]) == EXIT_VERIFIED

    def test_and_finds_a_bug_the_constructor_allows(self, tmp_path, capsys):
        p = tmp_path / "b.cpp"
        p.write_text(SOURCE.replace("data_[i % 4] = 0", "data_[i % 4] = 100 / (int)cap_"),
                     encoding="utf-8")
        code = main(["verify", str(p), "--function", "at", "--no-llm"])
        assert code == EXIT_COUNTEREXAMPLE
        assert "division by zero" in capsys.readouterr().out


class TestObjectParameters:
    """A class the harness cannot declare and fill is not filled."""

    @pytest.mark.parametrize("cls", ["Node", "Owned", "Sealed", "Ring"])
    def test_an_unfillable_class_is_refused_not_filled(self, tmp_path, cls):
        p = tmp_path / "p.cpp"
        p.write_text(SOURCE + f"\nint take(const {cls}* p) {{ return p != 0; }}\n",
                     encoding="utf-8")
        with pytest.raises(HarnessError, match="cannot construct"):
            generate(p, "take")

    def test_a_plain_struct_is_still_filled(self, tmp_path):
        p = tmp_path / "p.cpp"
        p.write_text("struct P { int x; struct P (*next)(void); };\n"
                     "int take(const P* p) { return p->x; }\n", encoding="utf-8")
        assert "p_obj.x = VERIPP_NONDET_INT();" in generate(p, "take").code

    def test_access_after_an_inline_member_function(self):
        from veripp.cppsig import find_struct

        info = find_struct(
            "class C { public: int a; bool empty() const { return a == 0; }\n"
            "private: int b; };", "C")
        assert [(f.name, f.access) for f in info.fields] == [("a", "public"), ("b", "private")]

    def test_an_anonymous_typedef_struct_is_public(self):
        from veripp.cppsig import find_struct

        info = find_struct("typedef struct { int a; } T;", "T")
        assert [f.access for f in info.fields] == ["public"]
