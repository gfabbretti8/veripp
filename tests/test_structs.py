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
    p.write_text(SOURCE, encoding="utf-8")
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
        , encoding="utf-8")
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
    , encoding="utf-8")
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
        p.write_text(self.SRC, encoding="utf-8")
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
        , encoding="utf-8")
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


class TestLibraryInitializers:
    """Build an object the way the library does, not field by field.

    Filling every field independently admits combinations the type's own
    invariants forbid, and those produce failures no caller could cause. On
    lodepng that was the largest remaining source of false leads, and 31 of
    the 50 affected types shipped their own constructor.
    """

    SRC = (
        '#include "veripp/contracts.hpp"\n'
        "typedef struct { unsigned char* data; size_t size; size_t allocsize; } vec_t;\n"
        "static void vec_init(vec_t* v) { v->data = 0; v->size = 0; v->allocsize = 0; }\n"
        "static size_t vec_size(vec_t* v) { return v->size; }\n"
    )

    def _harness(self, tmp_path, **kw):
        p = tmp_path / "s.c"
        p.write_text(self.SRC, encoding="utf-8")
        return generate(p, "vec_size", HarnessOptions(**kw))

    def test_the_initializer_is_called(self, tmp_path):
        code = self._harness(tmp_path).code
        assert "vec_init(&v_obj);" in code
        assert "v_obj.size = VERIPP_NONDET" not in code  # not also field-filled

    def test_the_narrower_question_is_disclosed(self, tmp_path):
        assumptions = self._harness(tmp_path).assumptions
        assert any("as `vec_init` leaves it" in a for a in assumptions)
        assert any("NOT explored" in a for a in assumptions)

    def test_it_can_be_turned_off(self, tmp_path):
        code = self._harness(tmp_path, use_initializers=False).code
        assert "vec_init(&v_obj);" not in code
        assert "v_obj.size" in code

    def test_an_initializer_is_not_called_on_itself(self, tmp_path):
        """Verifying vec_init must not begin by calling vec_init."""
        p = tmp_path / "s.c"
        p.write_text(self.SRC, encoding="utf-8")
        assert "vec_init(&v_obj);" not in generate(p, "vec_init").code

    def test_an_initializer_needing_more_arguments_is_not_a_drop_in(self, tmp_path):
        p = tmp_path / "s.c"
        p.write_text(
            '#include "veripp/contracts.hpp"\n'
            "typedef struct { int n; } box_t;\n"
            "static void box_init(box_t* b, int n) { b->n = n; }\n"
            "static int box_get(box_t* b) { return b->n; }\n"
        , encoding="utf-8")
        code = generate(p, "box_get").code
        assert "box_init" not in code
        assert "b_obj.n = VERIPP_NONDET_INT();" in code


class TestStructAliases:
    """`typedef struct json_value_t JSON_Value;` -- a C API's handle type.

    Looking up the alias found nothing while the definition sat in the same
    file under its tag, so parson looked like a library built on genuinely
    opaque handles: 19% of its functions reachable. It was 91%.
    """

    SRC = (
        "struct json_value_t { void* parent; int type; double num; };\n"
        "typedef struct json_value_t JSON_Value;\n"
        "typedef struct json_value_t *JSON_ValuePtr;\n"
    )

    def test_an_alias_resolves_to_its_tag(self):
        assert [f.name for f in find_struct(self.SRC, "JSON_Value").fields] == [
            "parent", "type", "num",
        ]

    def test_the_tag_itself_still_works(self):
        assert find_struct(self.SRC, "json_value_t").fields

    def test_a_pointer_alias_is_not_silently_unwrapped(self):
        """`typedef struct T *Alias;` names a pointer, a different type."""
        with pytest.raises(SignatureError):
            find_struct(self.SRC, "JSON_ValuePtr")

    def test_a_genuinely_opaque_type_is_still_refused(self):
        with pytest.raises(SignatureError, match="no definition") as exc:
            find_struct("typedef struct hidden_t Hidden;\n", "Hidden")
        # The message must name the alias the caller wrote, not only the tag.
        assert "Hidden" in str(exc.value) and "hidden_t" in str(exc.value)

    def test_an_aliased_parameter_can_be_harnessed(self, tmp_path):
        p = tmp_path / "s.c"
        p.write_text('#include "veripp/contracts.hpp"\n' + self.SRC +
                     "static int kind(JSON_Value* v) { return v->type; }\n", encoding="utf-8")
        code = generate(p, "kind").code
        assert "v_obj.type = VERIPP_NONDET_INT();" in code


class TestUnions:
    """C libraries hand out unions as handle types.

    `LZ4_stream_t` is `union LZ4_stream_u`, and a scanner that knows only
    class and struct refuses every function taking one -- 10 of lz4's.
    """

    SRC = (
        "union LZ4_stream_u { long long table[4]; void* p; };\n"
        "typedef union LZ4_stream_u LZ4_stream_t;\n"
    )

    def test_a_union_is_found(self):
        info = find_struct(self.SRC, "LZ4_stream_u")
        assert info.is_union
        assert [f.name for f in info.fields] == ["table", "p"]

    def test_a_union_alias_resolves(self):
        assert find_struct(self.SRC, "LZ4_stream_t").is_union

    def test_members_are_not_filled_one_by_one(self, tmp_path):
        """They share storage, so assigning each in turn would model only the
        last. Left alone, the object is nondeterministic bytes -- every member
        at once."""
        p = tmp_path / "s.c"
        p.write_text('#include "veripp/contracts.hpp"\n' + self.SRC +
                     "static int use(LZ4_stream_t* s) { return s->p ? 1 : 0; }\n", encoding="utf-8")
        harness = generate(p, "use")
        assert "s_obj.table" not in harness.code
        assert "s_obj.p =" not in harness.code
        assert any("union, left nondeterministic" in a for a in harness.assumptions)
        assert "LZ4_stream_t s_obj;" in harness.code


class TestLibraryConstructors:
    """Build an object by calling the functions that RETURN one.

    An initialiser fills a struct the caller already owns, so it only helps
    where the definition is in view. A constructor allocates and returns,
    which is how most C APIs hand out their handle types -- and it is the
    only way in when the definition is not visible at all.

    An earlier attempt at this picked one constructor by shape and measured
    worse. It should have: a type usually has several that build genuinely
    different objects, and choosing one narrows the question in a way no
    caller asked for. These tests pin that all of them are offered and the
    solver decides.
    """

    SRC = (
        '#include "veripp/contracts.hpp"\n'
        "typedef struct node_t node_t;\n"
        "struct node_t { int kind; node_t* next; };\n"
        "node_t* node_new_leaf(void) { return 0; }\n"
        "node_t* node_new_branch(void) { return 0; }\n"
        "int node_kind(node_t* n) { return n->kind; }\n"
    )

    def _harness(self, tmp_path, src=None, fn="node_kind", **kw):
        p = tmp_path / "s.c"
        p.write_text(src if src is not None else self.SRC, encoding="utf-8")
        kw.setdefault("use_constructors", True)
        return generate(p, fn, HarnessOptions(**kw))

    def test_every_constructor_is_offered_not_one(self, tmp_path):
        code = self._harness(tmp_path).code
        assert "node_new_leaf()" in code
        assert "node_new_branch()" in code
        assert "VERIPP_NONDET_INT() % 2" in code

    def test_a_null_return_is_assumed_away(self, tmp_path):
        """Allocation failure is a different question; the caller checks."""
        assert "VERIPP_ASSUME(n_obj != 0);" in self._harness(tmp_path).code

    def test_the_object_replaces_field_filling(self, tmp_path):
        code = self._harness(tmp_path).code
        assert "n_obj.kind" not in code
        assert "n_obj_next_target" not in code

    def test_the_narrower_question_is_disclosed(self, tmp_path):
        assumptions = self._harness(tmp_path).assumptions
        assert any("node_new_leaf" in a and "node_new_branch" in a
                   for a in assumptions)
        assert any("NOT explored" in a for a in assumptions)

    def test_it_is_off_by_default(self, tmp_path):
        code = self._harness(tmp_path, use_constructors=False).code
        assert "node_new_leaf" not in code
        assert "n_obj.kind" in code

    def test_a_constructor_is_not_called_on_itself(self, tmp_path):
        src = self.SRC + "node_t* node_new_from(node_t* n) { return n; }\n"
        code = self._harness(tmp_path, src=src, fn="node_new_from").code
        assert "node_new_from()" not in code

    def test_a_constructor_needing_arguments_is_not_one(self, tmp_path):
        src = (
            '#include "veripp/contracts.hpp"\n'
            "typedef struct box_t box_t;\n"
            "struct box_t { int n; };\n"
            "box_t* box_new(int n) { (void)n; return 0; }\n"
            "int box_get(box_t* b) { return b->n; }\n"
        )
        code = self._harness(tmp_path, src=src, fn="box_get").code
        assert "box_new" not in code
        assert "b_obj.n" in code

    def test_a_constructor_of_another_type_is_not_one(self, tmp_path):
        src = self.SRC + "int* int_new(void) { return 0; }\n"
        assert "int_new()" not in self._harness(tmp_path, src=src).code

    def test_a_constructor_that_is_only_declared_is_not_used(self, tmp_path):
        """Its allocation is in another translation unit, so it is not modelled.

        Calling it would hand the harness a pointer with nothing behind it,
        and every dereference downstream would fail on a buffer the real
        constructor would have allocated. That is a fabricated counterexample,
        which is worse than no coverage. Link the defining source instead.
        """
        src = (
            '#include "veripp/contracts.hpp"\n'
            "typedef struct ctx_t ctx_t;\n"
            "struct ctx_t { int step; };\n"
            "ctx_t* ctx_new(void);\n"
            "int drive(ctx_t* c) { return c->step; }\n"
        )
        code = self._harness(tmp_path, src=src, fn="drive").code
        assert "ctx_new()" not in code
        assert "c_obj.step" in code


class TestConstructorChains:
    """Half of a C API's handle types are never returned by a constructor.

    parson's JSON_Value has three; JSON_Object and JSON_Array have none, and
    you get one by building a value and asking it. Nineteen of that file's
    functions take exactly those two types, so stopping at the direct case
    leaves the larger half of the API filling fields at random.
    """

    SRC = (
        '#include "veripp/contracts.hpp"\n'
        "typedef struct doc_t doc_t;\n"
        "typedef struct row_t row_t;\n"
        "struct row_t { int n; };\n"
        "struct doc_t { row_t row; };\n"
        "doc_t* doc_new(void) { return 0; }\n"
        "void doc_free(doc_t* d) { (void)d; }\n"
        "row_t* doc_row(doc_t* d) { return &d->row; }\n"
        "int row_n(row_t* r) { return r->n; }\n"
    )

    def _harness(self, tmp_path, src=None, fn="row_n"):
        p = tmp_path / "s.c"
        p.write_text(src if src is not None else self.SRC, encoding="utf-8")
        return generate(p, fn, HarnessOptions(use_constructors=True))

    def test_the_owner_is_built_and_then_asked(self, tmp_path):
        code = self._harness(tmp_path).code
        assert "doc_new()" in code
        assert "row_t *r_obj = doc_row(r_src);" in code

    def test_the_accessor_result_is_assumed_non_null(self, tmp_path):
        """A caller holding one got it the same way, so it is not null."""
        assert "VERIPP_ASSUME(r_obj != 0);" in self._harness(tmp_path).code

    def test_the_owner_is_freed_not_the_part(self, tmp_path):
        code = self._harness(tmp_path).code
        assert "doc_free(r_src);" in code
        assert "doc_free(r_obj);" not in code

    def test_both_steps_are_disclosed(self, tmp_path):
        assumptions = self._harness(tmp_path).assumptions
        assert any("doc_row" in a and "doc_new" in a for a in assumptions)

    def test_a_direct_constructor_wins_over_a_chain(self, tmp_path):
        """One step is a smaller assumption than two."""
        src = self.SRC + "row_t* row_new(void) { return 0; }\n"
        code = self._harness(tmp_path, src=src).code
        assert "row_new()" in code
        assert "doc_row" not in code

    def test_a_same_type_accessor_is_a_walk_not_a_construction(self, tmp_path):
        src = (
            '#include "veripp/contracts.hpp"\n'
            "typedef struct node_t node_t;\n"
            "struct node_t { node_t* next; int n; };\n"
            "node_t* node_next(node_t* n) { return n->next; }\n"
            "int node_n(node_t* n) { return n->n; }\n"
        )
        assert "node_next(" not in self._harness(tmp_path, src=src, fn="node_n").code


class TestCursorInsideItsBuffer:
    """content + length + offset: the offset is inside the buffer.

    Filled independently the solver picks `length = 4, offset = 2**64-1` and
    blames the library for the read. After cJSON's allocator was resolved,
    that one combination was behind most of the counterexamples left in the
    file.

    The assumption is about the state the function is HANDED. A function that
    advances the cursor past the end is still caught, because the check is on
    what it does, not on what it was given.
    """

    SRC = (
        '#include "veripp/contracts.hpp"\n'
        "typedef struct { unsigned char *content; unsigned long length;"
        " unsigned long offset; } buf_t;\n"
        "int peek(buf_t *b) { return b->content[b->offset]; }\n"
    )

    def _harness(self, tmp_path, src=None, fn="peek"):
        p = tmp_path / "s.c"
        p.write_text(src if src is not None else self.SRC, encoding="utf-8")
        return generate(p, fn)

    def test_the_cursor_is_bounded_by_the_length(self, tmp_path):
        assert ("VERIPP_ASSUME(b_obj.offset <= b_obj.length);"
                in self._harness(tmp_path).code)

    def test_it_is_disclosed_as_the_caller_s_invariant(self, tmp_path):
        assumptions = self._harness(tmp_path).assumptions
        assert any("cursor is inside the buffer" in a for a in assumptions)

    def test_a_struct_with_no_counted_buffer_gets_no_such_claim(self, tmp_path):
        src = (
            '#include "veripp/contracts.hpp"\n'
            "typedef struct { int a; unsigned long offset; } plain_t;\n"
            "int get(plain_t *p) { return (int)p->offset; }\n"
        )
        assert "offset <=" not in self._harness(tmp_path, src=src, fn="get").code

    def test_two_lengths_are_too_ambiguous_to_pair(self, tmp_path):
        """Say nothing rather than guess which length the cursor belongs to."""
        src = (
            '#include "veripp/contracts.hpp"\n'
            "typedef struct { unsigned char *in; unsigned long in_len;"
            " unsigned char *out; unsigned long out_len;"
            " unsigned long offset; } io_t;\n"
            "int peek(io_t *b) { return b->in[b->offset]; }\n"
        )
        assert "offset <=" not in self._harness(tmp_path, src=src).code
