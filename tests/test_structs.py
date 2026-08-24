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
