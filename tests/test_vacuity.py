"""Vacuity: an unreachable harness proves everything and means nothing.

ESBMC answers "does the property hold under these assumptions". It cannot
notice that the assumptions are unsatisfiable, and neither can the model that
proposed them -- a weak model fails toward over-constraining, and the solver
applauds. This is the mechanical guard against that.
"""

import pytest

from veripp.cli import EXIT_INCONCLUSIVE, EXIT_VERIFIED, main
from veripp.harness import reachability_variant

SOURCE = """\
#include "veripp/contracts.hpp"
int div_it(int a, int b) { return a / b; }
"""


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "v.cpp"
    p.write_text(SOURCE, encoding="utf-8")
    return p


def test_probe_asserts_falsehood_before_returning():
    code = "int main() {\n    (void)f(1);\n    return 0;\n}\n"
    probed = reachability_variant(code)
    assert "VERIPP_ASSERT(0 &&" in probed
    assert probed.index("VERIPP_ASSERT") < probed.index("return 0;")


def test_probe_is_a_no_op_without_a_return():
    assert "static_assert" in reachability_variant("int main() {}\n")


@pytest.mark.esbmc
class TestVacuityEndToEnd:
    def _run(self, capsys, src, *assumes):
        argv = ["verify", str(src), "--function", "div_it", "--no-llm", "--timeout", "120"]
        for a in assumes:
            argv += ["--assume", a]
        code = main(argv)
        return code, capsys.readouterr().out

    def test_satisfiable_preconditions_give_a_real_proof(self, capsys, src):
        code, out = self._run(capsys, src, "b > 0", "a > -100 && a < 100")
        assert code == EXIT_VERIFIED
        assert "VACUOUS" not in out
        assert "requires b > 0" in out

    def test_contradictory_preconditions_are_not_a_pass(self, capsys, src):
        code, out = self._run(capsys, src, "b > 0 && b < 0")
        assert code == EXIT_INCONCLUSIVE
        assert "VACUOUS" in out
        assert "NOT a proof" in out

    def test_a_harness_with_no_assumptions_skips_the_extra_run(self, capsys, tmp_path):
        # Nothing to contradict, so no probe is needed and none is reported.
        p = tmp_path / "n.cpp"
        p.write_text('#include "veripp/contracts.hpp"\nint id(int x) { return x; }\n', encoding="utf-8")
        code = main(["verify", str(p), "--function", "id", "--no-llm", "--timeout", "120"])
        assert code == EXIT_VERIFIED
        assert "VACUOUS" not in capsys.readouterr().out
