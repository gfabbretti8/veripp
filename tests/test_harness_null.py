"""A NULL dereference of a field the harness itself left null.

The harness cuts pointer fields to null at --max-struct-depth, and leaves one
null when it cannot construct the pointee; both are recorded as assumptions.
A NULL dereference of that very field is then the harness's null, not one a
caller was shown to produce. It is still reported -- an unchecked pointer is
a real bug class and only a caller can say whether the field is ever null --
but it is said, named correctly, and ranked after the nulls the code
produced on its own. parson's ten NULL-pointer counterexamples were all this
one shape, and the note named a different null field than the one
dereferenced.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from veripp.esbmc import SourceLoc, ViolatedProperty
from veripp.triage import harness_null_field, harness_null_note

ASSUMPTIONS = [
    "pointer field `array_obj_wrapping_value_target.parent` is null (struct depth bound 2 reached); deeper object graphs are NOT explored",
    "pointer field `array_obj.items` is null: `JSON_Value *` is not a type veripp can construct here",
    "`array` points to one `JSON_Array` with every field nondeterministic",
]


def _prop(tmp_path, line_text, description="dereference failure: NULL pointer"):
    src = tmp_path / "p.c"
    src.write_text("int unrelated;\n" + line_text + "\n", encoding="utf-8")
    return ViolatedProperty(loc=SourceLoc(str(src), 2), description=description)


class TestMatching:
    def test_the_field_the_line_dereferences_is_the_one_named(self, tmp_path):
        prop = _prop(tmp_path, "    return array->items[index];")
        assert harness_null_field(prop, ASSUMPTIONS) == ("items", "type")
        assert harness_null_note(prop, ASSUMPTIONS) == (
            "dereferences `items`, which the harness left null (its type could not be constructed)"
        )

    def test_a_depth_cut_is_told_apart_from_an_unconstructible_type(self, tmp_path):
        prop = _prop(tmp_path, "    return v->parent->type;")
        assert harness_null_field(prop, ASSUMPTIONS) == ("parent", "depth")
        assert "cut at --max-struct-depth" in harness_null_note(prop, ASSUMPTIONS)

    def test_a_line_that_touches_no_null_field_is_not_ours(self, tmp_path):
        prop = _prop(tmp_path, "    return *p;")
        assert harness_null_field(prop, ASSUMPTIONS) is None

    def test_the_field_name_inside_a_comment_or_string_does_not_count(self, tmp_path):
        prop = _prop(tmp_path, '    return *p; /* items */')
        assert harness_null_field(prop, ASSUMPTIONS) is None

    def test_only_null_pointer_properties_are_considered(self, tmp_path):
        prop = _prop(tmp_path, "    return array->items[index];",
                     description="dereference failure: array bounds violated")
        assert harness_null_field(prop, ASSUMPTIONS) is None

    def test_no_assumptions_no_match(self, tmp_path):
        prop = _prop(tmp_path, "    return array->items[index];")
        assert harness_null_field(prop, []) is None
        assert harness_null_field(None, ASSUMPTIONS) is None


class TestRanking:
    def test_a_harness_null_sorts_after_the_codes_own_nulls(self):
        from veripp.scan import FunctionResult, ScanReport

        report = ScanReport(source=Path("f.c"), candidates=2)
        report.results = [
            FunctionResult(name="a_ours", outcome="counterexample", detail="NULL",
                           linkage="external", harness_null="dereferences `items`, which the harness left null (x)"),
            FunctionResult(name="b_theirs", outcome="counterexample", detail="NULL",
                           linkage="static", callers=1),
        ]
        summary = report.summary()
        assert summary.index("b_theirs: ") < summary.index("a_ours: ")
        assert "a_ours: NULL  [external; dereferences `items`, which the harness left null (x)]" in summary


NODE = """\
typedef struct Node { struct Node *next; int v; } Node;

int second(Node *n) { return n->next->v; }
int first(Node *n) { return n->v; }
int deref(int *p) { return *p; }
"""


@pytest.mark.esbmc
class TestEndToEnd:
    def test_scan_names_the_cut_field_and_verify_notes_it(self, tmp_path, capsys):
        from veripp.cli import main
        from veripp.esbmc import VerifyConfig
        from veripp.harness import HarnessOptions
        from veripp.paths import contracts_include_dir
        from veripp.scan import scan

        src = tmp_path / "node.c"
        src.write_text(NODE, encoding="utf-8")
        config = VerifyConfig(timeout_s=90, include_dirs=[contracts_include_dir()])
        report = scan(src, config, HarnessOptions(max_struct_depth=0), jobs=2, retry_budget=0)
        found = {r.name: r for r in report.counterexamples}
        assert "second" in found, report.summary()
        assert found["second"].harness_null is not None
        assert "`next`" in found["second"].harness_null
        assert "cut at --max-struct-depth" in found["second"].harness_null
        assert found["first"].harness_null is None if "first" in found else True

        code = main(["verify", str(src), "--function", "second", "--no-llm",
                     "--max-struct-depth", "0"])
        out = capsys.readouterr().out
        assert code == 1
        assert "the failing line dereferences `next`, which the harness left null" in out
