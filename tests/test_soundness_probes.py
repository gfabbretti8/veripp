"""The soundness probes: planted bugs a checker must reject, for the right reason.

There were two probes, both for array bounds, and a probe passed on the words
"VERIFICATION FAILED" -- so a checker that missed every other default check
passed, and so did one that rejected a probe for a reason that had nothing to
do with it. There is now one planted bug per default check, run under exactly
the checks a verification uses, and a probe passes only when the property it
plants is the one that fails. The results are remembered per checker, by the
checker's bytes, since `verify` asks after every proof.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from veripp.esbmc import SOUNDNESS_PROBES, VerifyConfig, check_soundness

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fake checkers are sh scripts")


@pytest.fixture(autouse=True)
def own_state(tmp_path, monkeypatch):
    """Remembered results live beside the managed checker; keep them here."""
    monkeypatch.setenv("VERIPP_CHECKER_DIR", str(tmp_path / "state" / "checker"))


def _fake_checker(tmp_path: Path, output: str, name: str = "esbmc") -> Path:
    """A 'checker' that prints `output` for every program, and logs each run."""
    script = tmp_path / name
    log = tmp_path / f"{name}.runs"
    script.write_text(
        f"#!/bin/sh\necho run >> '{log}'\ncat <<'OUT'\n{output}\nOUT\n", encoding="utf-8"
    )
    script.chmod(0o755)
    return script


def _runs(tmp_path: Path, name: str = "esbmc") -> int:
    log = tmp_path / f"{name}.runs"
    return len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0


def test_every_default_check_has_a_planted_bug():
    """The checks VerifyConfig turns on, and the probes that exercise them."""
    config = VerifyConfig()
    on = {name for name, enabled in (
        ("overflow", config.overflow_check), ("bounds", config.bounds_check),
        ("pointer", config.pointer_check), ("division", config.div_by_zero_check),
        ("leak", config.memory_leak_check), ("uninitialised", config.uninitialised_check),
        ("shift", config.ub_shift_check), ("nan", config.nan_check),
    ) if enabled}
    covered = {
        "overflow": "arithmetic overflow", "bounds": "local-array bounds",
        "pointer": "null pointer", "division": "division by zero",
        "leak": "memory leak", "uninitialised": "uninitialised read",
        "shift": "undefined shift", "nan": "NaN",
    }
    for check in on:
        assert covered[check] in SOUNDNESS_PROBES, f"no probe for the {check} check"


@pytest.mark.esbmc
def test_a_sound_checker_rejects_each_for_its_own_reason():
    results = check_soundness()
    assert set(results) == set(SOUNDNESS_PROBES)
    assert all(results.values()), [name for name, ok in results.items() if not ok]


@posix_only
def test_a_rejection_for_the_wrong_reason_is_not_a_pass(tmp_path):
    """A checker that fails every program on some unrelated property used to
    pass every probe: only the verdict was read."""
    checker = _fake_checker(tmp_path, (
        "Violated property:\n"
        "  file probe.c line 1 column 1 function main\n"
        "  something that is not the planted bug\n"
        "\n"
        "VERIFICATION FAILED"
    ))
    results = check_soundness(str(checker))
    assert not any(results.values()), results


@posix_only
@pytest.mark.esbmc
def test_a_check_that_is_off_is_caught(tmp_path):
    """The two bounds probes cannot see a checker that ignores division."""
    import shutil

    from veripp.esbmc import find_esbmc

    real = shutil.which("esbmc") or find_esbmc()
    wrapper = tmp_path / "esbmc-without-div"
    wrapper.write_text(f'#!/bin/sh\nexec "{real}" "$@" --no-div-by-zero-check\n',
                       encoding="utf-8")
    wrapper.chmod(0o755)
    results = check_soundness(str(wrapper))
    assert results["division by zero"] is False
    assert all(ok for name, ok in results.items() if name != "division by zero")


@posix_only
def test_results_are_remembered_by_the_checkers_bytes(tmp_path):
    checker = _fake_checker(tmp_path, "VERIFICATION SUCCESSFUL")
    first = check_soundness(str(checker), remember=True)
    asked = _runs(tmp_path)
    assert asked == len(SOUNDNESS_PROBES)
    assert check_soundness(str(checker), remember=True) == first
    assert _runs(tmp_path) == asked, "the same checker was probed again"

    # A different checker at the same path is a different checker.
    checker.write_text(checker.read_text(encoding="utf-8") + "# rebuilt\n",
                       encoding="utf-8")
    check_soundness(str(checker), remember=True)
    assert _runs(tmp_path) == 2 * asked


@posix_only
def test_doctor_asks_afresh(tmp_path):
    """Remembering is for the check after a proof; asking is doctor's job."""
    checker = _fake_checker(tmp_path, "VERIFICATION SUCCESSFUL")
    check_soundness(str(checker), remember=True)
    asked = _runs(tmp_path)
    check_soundness(str(checker))
    assert _runs(tmp_path) == 2 * asked


def test_verify_uses_the_remembered_results(monkeypatch, tmp_path):
    from veripp import cli
    from veripp.agent import AgentReport
    from veripp.esbmc import Outcome, VerifyResult

    asked: list[dict] = []
    monkeypatch.setattr(cli, "check_soundness",
                        lambda *a, **kw: asked.append(kw) or {})
    monkeypatch.setattr(cli, "verify_with_agent", lambda source, config, **kw:
                        AgentReport(final=VerifyResult(Outcome.VERIFIED, config)))
    src = tmp_path / "f.c"
    src.write_text("int f(int x) { return x; }\n", encoding="utf-8")
    cli.main(["verify", str(src), "--function", "f", "--no-llm"])
    assert asked == [{"remember": True}]
