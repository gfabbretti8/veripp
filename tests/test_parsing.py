from veripp.esbmc import Outcome, VerifyConfig, _parse_output

CFG = VerifyConfig()

SUCCESS = "Symex completed\nVERIFICATION SUCCESSFUL\n"
FAILED = """Counterexample:

State 3 file off_by_one.cpp line 7 function sum_array
----------------------------------------------------
  i = 4

Violated property:
  file off_by_one.cpp line 7 function sum_array
  array bounds violated: array `a' upper bound

VERIFICATION FAILED
"""
UNWIND = "unwinding assertion loop 0\n"


def test_success():
    assert _parse_output(SUCCESS, CFG).outcome is Outcome.VERIFIED


def test_counterexample():
    r = _parse_output(FAILED, CFG)
    assert r.outcome is Outcome.COUNTEREXAMPLE
    assert "array bounds" in (r.violated_property or "")
    assert r.trace and r.trace[0].line == 7


def test_unwind_limit_is_not_a_bug():
    assert _parse_output(UNWIND, CFG).outcome is Outcome.UNWIND_LIMIT
