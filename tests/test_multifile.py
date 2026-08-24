"""Real projects: compile_commands.json, linking, and unresolved callees.

A single file is rarely self-contained. The build system already knows the
include paths, defines and standard each TU needs; and linking the TU that
defines a callee is a soundness matter, because ESBMC havocs an undefined
function's return value but assumes it does not write through its pointer
arguments.
"""

import json

import pytest

from veripp.compdb import CompDBError, entry_for, find_database, sources
from veripp.cppsig import unresolved_callees

HEADER = """\
#pragma once
struct Box { int w; int h; };
void normalize(Box* b);
int area(Box* b);
"""
HELPER = """\
#include "geom.h"
void normalize(Box* b) { if (b->w < 0) b->w = 0; if (b->h < 0) b->h = 0; }
"""
AREA = """\
#include "geom.h"
#include "veripp/contracts.hpp"
int area(Box* b) {
    normalize(b);
    VERIPP_ASSUME(b->w < 40000 && b->h < 40000);
    return b->w * b->h;
}
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "include").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "build").mkdir()
    (tmp_path / "include" / "geom.h").write_text(HEADER)
    (tmp_path / "src" / "helper.cpp").write_text(HELPER)
    (tmp_path / "src" / "area.cpp").write_text(AREA)
    db = [
        {
            "directory": str(tmp_path),
            "file": str(tmp_path / "src" / f),
            "command": (
                f"c++ -std=c++17 -I{tmp_path}/include -DPROJ=1 -O2 -Wall "
                f"-c {tmp_path}/src/{f} -o build/{f}.o"
            ),
        }
        for f in ("area.cpp", "helper.cpp")
    ]
    (tmp_path / "build" / "compile_commands.json").write_text(json.dumps(db))
    return tmp_path


class TestCompilationDatabase:
    def test_discovered_in_a_build_subdirectory(self, project):
        assert find_database(project / "src" / "area.cpp") == (
            project / "build" / "compile_commands.json"
        )

    def test_extracts_only_what_a_model_checker_can_use(self, project):
        entry = entry_for(project / "build" / "compile_commands.json",
                          project / "src" / "area.cpp")
        assert entry.include_dirs == [project / "include"]
        assert entry.defines == ["PROJ=1"]
        assert entry.std == "c++17"
        # -O2, -Wall, -c, -o are about producing an object file.
        assert not any("-O2" in a or "-Wall" in a for a in entry.esbmc_args())

    def test_arguments_form_is_supported(self, tmp_path):
        db = tmp_path / "compile_commands.json"
        src = tmp_path / "a.c"
        src.write_text("int main(void){return 0;}")
        db.write_text(json.dumps([{
            "directory": str(tmp_path), "file": "a.c",
            "arguments": ["cc", "-std=c11", "-I./inc", "-DX=2", "-c", "a.c"],
        }]))
        entry = entry_for(db, src)
        assert entry.std == "c11"
        assert entry.defines == ["X=2"]
        assert entry.include_dirs == [(tmp_path / "inc").resolve()]

    def test_a_file_outside_the_database_says_so_usefully(self, project):
        with pytest.raises(CompDBError, match="not in"):
            entry_for(project / "build" / "compile_commands.json",
                      project / "include" / "geom.h")

    def test_lists_translation_units(self, project):
        found = {p.name for p in sources(project / "build" / "compile_commands.json")}
        assert found == {"area.cpp", "helper.cpp"}


class TestUnresolvedCallees:
    def test_declared_but_not_defined_is_reported(self):
        assert unresolved_callees(HEADER + AREA, "normalize(b); return 0;") == ["normalize"]

    def test_definition_present_means_resolved(self):
        assert unresolved_callees(HEADER + AREA + HELPER, "normalize(b); return 0;") == []

    def test_macros_are_not_calls(self):
        src = "#define LOG(x) ((void)0)\nint f(int a) { return a; }\n"
        assert unresolved_callees(src, "LOG(1); return f(2);") == []


@pytest.mark.esbmc
class TestLinkingChangesTheAnswer:
    """The soundness point, end to end."""

    def _run(self, capsys, project, *argv):
        from veripp.cli import main

        code = main([
            "verify", str(project / "src" / "area.cpp"), "--function", "area",
            "--no-llm", "--timeout", "180", *argv,
        ])
        return code, capsys.readouterr().out

    def test_unlinked_callee_is_disclosed(self, capsys, project):
        code, out = self._run(capsys, project)
        assert "normalize" in out
        assert "not defined in this translation unit" in out

    def test_linking_the_definition_changes_the_verdict(self, capsys, project):
        # Unlinked, normalize()'s clamp is not modelled, so the multiply
        # overflows; linked, it is, and the function verifies.
        unlinked, _ = self._run(capsys, project)
        linked, out = self._run(capsys, project, "--link", str(project / "src" / "helper.cpp"))
        assert unlinked == 1 and linked == 0
        assert "not defined in this translation unit" not in out

    def test_database_flags_are_actually_used(self, capsys, project):
        # Without -I include, geom.h is unfindable and nothing would compile.
        code, out = self._run(capsys, project, "--link", str(project / "src" / "helper.cpp"))
        assert code == 0
