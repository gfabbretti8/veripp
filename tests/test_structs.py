"""Struct and object parameters: the largest measured coverage ceiling.

Two thirds of lodepng's functions were refused for taking a pointer to a
non-scalar. These tests pin how such a parameter gets built, and that every
simplification made along the way is disclosed rather than assumed away.
"""

import pytest

from veripp.cppsig import SignatureError, find_struct
from veripp.harness import HarnessOptions, generate

SOURCE = """\
#include "veripp/contracts.hpp"

struct Inner { int x; double y; };

struct Widget {
    int count;
    char name[8];
    Inner inner;
    Widget* next;
    int a, b;
    static int shared;
};

int head(Widget* w)   { return w->count > 0 ? 1 : 0; }
int avg(Widget* w)    { return w->inner.x / w->count; }
int byref(Widget& w)  { return w.count; }
"""


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "s.cpp"
    p.write_text(SOURCE)
    return p


class TestFields:
    def test_reads_every_data_member_in_order(self):
        info = find_struct(SOURCE, "Widget")
        assert [f.name for f in info.fields] == ["count", "name", "inner", "next", "a", "b"]
        assert info.fields[1].array_len == "8"
        assert info.fields[3].is_pointer

    def test_static_members_are_not_per_object(self):
        assert "shared" not in {f.name for f in find_struct(SOURCE, "Widget").fields}

    def test_opaque_type_is_refused_with_a_usable_message(self):
        with pytest.raises(SignatureError, match="opaque|not visible"):
            find_struct("struct Hidden;\nint f(struct Hidden* h);", "Hidden")


class TestObjectParameters:
    def test_pointer_parameter_is_built_and_passed(self, src):
        code = generate(src, "head").code
        assert "Widget w_obj;" in code
        assert "w_obj.count = VERIPP_NONDET_INT();" in code
        assert "Widget* w = &w_obj;" in code

    def test_nested_struct_fields_are_filled(self, src):
        code = generate(src, "head").code
        assert "w_obj.inner.x = VERIPP_NONDET_INT();" in code
        assert "w_obj.inner.y = VERIPP_NONDET_DOUBLE();" in code

    def test_array_fields_are_filled_by_loop(self, src):
        assert "w_obj.name[veripp_i_name] = VERIPP_NONDET_CHAR();" in generate(src, "head").code

    def test_self_referential_pointer_is_depth_bounded_and_disclosed(self, src):
        harness = generate(src, "head", HarnessOptions(max_struct_depth=1))
        assert harness.code.count("_target") > 0
        assert any("depth bound 1 reached" in a for a in harness.assumptions)
        # the chain must terminate
        assert "= 0;" in harness.code

    def test_reference_parameter_binds_to_the_object(self, src):
        code = generate(src, "byref").code
        assert "Widget& w = w_obj;" in code

    def test_nondeterministic_object_state_is_disclosed(self, src):
        harness = generate(src, "head")
        assert any(
            "every field nondeterministic" in a and "unreachable object state" in a
            for a in harness.assumptions
        )


class TestPreconditionsOverFields:
    def test_field_access_is_allowed(self, src):
        code = generate(src, "avg", extra_preconditions=["w->count != 0"]).code
        assert "VERIPP_ASSUME(w->count != 0);" in code

    def test_unknown_root_is_still_refused(self, src):
        from veripp.harness import HarnessError

        with pytest.raises(HarnessError, match="g_limit"):
            generate(src, "avg", extra_preconditions=["g_limit != 0"])


@pytest.mark.esbmc
class TestObjectHarnessEndToEnd:
    def _run(self, capsys, src, *argv):
        from veripp.cli import main

        code = main(["verify", str(src), "--no-llm", "--timeout", "180", *argv])
        return code, capsys.readouterr().out

    def test_safe_function_over_all_object_states(self, capsys, src):
        code, out = self._run(capsys, src, "--function", "head")
        assert code == 0
        assert "every field nondeterministic" in out

    def test_unconstrained_field_yields_a_counterexample(self, capsys, src):
        code, out = self._run(capsys, src, "--function", "avg")
        assert code == 1
        assert "overflow" in out or "division by zero" in out

    def test_field_preconditions_make_it_verify(self, capsys, src):
        code, out = self._run(
            capsys, src, "--function", "avg",
            "--assume", "w->count > 0",
            "--assume", "w->inner.x > -1000 && w->inner.x < 1000",
        )
        assert code == 0
        assert "CONDITIONAL" in out
        assert "requires w->count > 0" in out


class TestFieldTypeHygiene:
    def test_comments_do_not_leak_into_field_types(self):
        src = (
            "struct S {\n"
            "    int a;      /*red component*/\n"
            "    /*header*/ unsigned b;\n"
            "};\n"
        )
        types = {f.name: f.type for f in find_struct(src, "S").fields}
        assert types == {"a": "int", "b": "unsigned"}

    def test_enums_are_integers_a_harness_can_fill(self, tmp_path):
        p = tmp_path / "s.cpp"
        p.write_text(
            '#include "veripp/contracts.hpp"\n'
            "typedef enum Mode { OFF = 0, ON = 1 } Mode;\n"
            "struct Cfg { Mode mode; int n; };\n"
            "int use(Cfg* c) { return c->mode == ON ? c->n : 0; }\n"
        )
        code = generate(p, "use").code
        assert "c_obj.mode = (Mode)VERIPP_NONDET_INT();" in code
        assert "left uninitialised" not in code


def test_struct_pointer_next_to_a_size_is_one_object_not_an_array(tmp_path):
    """`ucvector_reserve(ucvector* p, size_t size)` grows p's capacity; `size`
    does not describe p's length. Pairing them built an array of structs and
    refused the function. Buffers are arrays of scalars; struct pointers are
    objects."""
    p = tmp_path / "s.c"
    p.write_text(
        '#include "veripp/contracts.hpp"\n'
        "typedef struct { unsigned char* data; size_t size; } vec_t;\n"
        "static int reserve(vec_t* v, size_t size) { return v->size < size; }\n"
        "static int total(const int* items, size_t size) {\n"
        "    int s = 0; for (size_t i = 0; i < size; ++i) s += items[i]; return s; }\n"
    )
    struct_harness = generate(p, "reserve")
    assert "vec_t v_obj;" in struct_harness.code
    assert "v_buf" not in struct_harness.code          # not treated as an array

    # A scalar pointee next to a length is still a buffer.
    buffer_harness = generate(p, "total")
    assert "items_buf" in buffer_harness.code
    assert any("harness bound on array length" in a for a in buffer_harness.assumptions)


class TestCountedPointerFields:
    """`size_t itext_num` beside `char** itext_keys`, read one by the other.

    Filling them independently -- one element, and a count up to 2**64 --
    guarantees an out-of-bounds read no caller could cause. This was the
    largest single source of false findings on lodepng.
    """

    SRC = (
        '#include "veripp/contracts.hpp"\n'
        "typedef struct {\n"
        "  size_t itext_num; char** itext_keys;\n"
        "  unsigned* data;   size_t data_size;\n"
        "} Info;\n"
        "static void walk(Info* i) {\n"
        "  for (size_t k = 0; k != i->itext_num; ++k) (void)i->itext_keys[k];\n"
        "  for (size_t k = 0; k != i->data_size; ++k) (void)i->data[k];\n"
        "}\n"
    )

    def _code(self, tmp_path):
        p = tmp_path / "s.c"
        p.write_text(self.SRC)
        return generate(p, "walk").code

    def test_the_buffer_gets_real_elements(self, tmp_path):
        code = self._code(tmp_path)
        assert "info_obj" not in code  # parameter is named `i` here
        assert "_itext_keys_items[4]" in code
        assert "_data_items[4]" in code

    def test_the_count_is_bounded_to_match(self, tmp_path):
        code = self._code(tmp_path)
        assert "VERIPP_ASSUME(i_obj.itext_num <= 4);" in code
        assert "VERIPP_ASSUME(i_obj.data_size <= 4);" in code

    def test_the_bound_comes_after_the_count_is_assigned(self, tmp_path):
        """A bound written before the nondet assignment is silently lost."""
        lines = self._code(tmp_path).splitlines()
        assigned = max(i for i, l in enumerate(lines) if "data_size = VERIPP_NONDET" in l)
        bounded = min(i for i, l in enumerate(lines) if "VERIPP_ASSUME" in l)
        assert bounded > assigned

    def test_pointer_elements_are_null_so_freeing_them_is_safe(self, tmp_path):
        assert "_itext_keys_items[veripp_k] = 0;" in self._code(tmp_path)

    def test_a_self_referential_pointer_is_not_a_counted_buffer(self, tmp_path):
        """`Widget* next` beside a `count` field is a chain, not an array."""
        p = tmp_path / "n.c"
        p.write_text(
            '#include "veripp/contracts.hpp"\n'
            "typedef struct Node { int count; struct Node* next; } Node;\n"
            "static int walk(Node* n) { return n->next ? n->next->count : 0; }\n"
        )
        code = generate(p, "walk").code
        assert "_target" in code          # the chain is followed
        assert "_next_items" not in code  # not turned into an array


class TestElaboratedTypeFields:
    """C spells types `struct Node* next;`, and that is a field.

    The scanner skipped any statement beginning with `struct`, treating it as
    a nested type definition -- so C code that spells its types the C way lost
    those fields silently, and the harness left them uninitialised.
    """

    SRC = (
        "typedef struct Node {\n"
        "  int count;\n"
        "  struct Node* next;\n"
        "  struct Inner { int z; } nested;\n"
        "  union U { int a; float b; } choice;\n"
        "} Node;\n"
    )

    def test_a_field_with_an_elaborated_type_is_kept(self):
        names = [f.name for f in find_struct(self.SRC, "Node").fields]
        assert "next" in names
        assert names[:2] == ["count", "next"]

    def test_a_nested_type_definition_is_still_skipped(self):
        # `struct Inner { int z; } nested;` defines a type and a member; the
        # definition must not be mistaken for a field of its own.
        names = [f.name for f in find_struct(self.SRC, "Node").fields]
        assert "Inner" not in names and "U" not in names

    def test_elaborated_and_plain_spellings_are_the_same_type(self):
        from veripp.harness import normalize_type

        assert normalize_type("struct Node") == normalize_type("Node") == "Node"
