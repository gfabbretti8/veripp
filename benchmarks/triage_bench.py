#!/usr/bin/env python3
"""Grade triage on findings whose answer is already written down.

eval_triage.py needs an API key and has two cases. This has more cases and
needs no key: it separates *what veripp asks* from *who answers*.

    triage_bench.py capture CORPUS OUT     # run each case, save veripp's prompts
    triage_bench.py grade   CORPUS OUT --model NAME
                                           # replay saved answers through veripp

`capture` runs every case to its counterexample with a model that records
what it is asked, and writes the prompts veripp would have sent -- the same
system text and the same context, byte for byte -- to OUT/prompts/.

Something then answers them: an API, a person, an agent. Each answer is a
file, OUT/answers/NAME/<case>.classify.txt and, where the answer is not
real_bug, <case>.precondition.txt.

`grade` replays those files through veripp's own loop, so the reply is read
by the parser a provider's reply is read by and a proposed precondition is
checked by ESBMC, and scores the result against the ground truth below.

The ground truth is not this file's opinion. Every case cites the record
that established it, by reading call sites and, for the real ones, by a
sanitizer run.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from veripp.agent import Budget, verify_with_agent  # noqa: E402
from veripp.esbmc import VerifyConfig  # noqa: E402
from veripp.harness import HarnessOptions, generate  # noqa: E402
from veripp.llm import LLMError, PromptedLLM  # noqa: E402
from veripp.paths import contracts_include_dir, scratch_dir  # noqa: E402
from veripp.triage import TargetInfo  # noqa: E402

REAL, NOT_REAL = "real", "not_real"


@dataclass
class Case:
    repo: str            # github owner/name, cloned at CORPUS/<name>
    rel: str             # source file, relative to CORPUS
    function: str
    truth: str           # REAL or NOT_REAL
    why: str             # the fact that decides it
    record: str          # where that was established
    includes: list[str] = field(default_factory=list)   # relative to CORPUS
    defines: list[str] = field(default_factory=list)
    unterminated: bool = False   # --unterminated: char* inputs are raw bytes

    @property
    def name(self) -> str:
        return f"{Path(self.rel).stem}.{self.function}"


FIELD = "benchmarks/HUNT_FIELD_REPORT.md"
REPRO = "veripp/benchmarks/repro"

# Only findings that reach the model today are listed. Others in the field
# report no longer do, which is the tool improving and not a gap here: the
# harness now refuses or proves them (is_decimal, find_builtin, cJSON_Minify,
# cJSON_DetachItemViaPointer), or needs a build configuration this script
# does not carry (mppe_rekey, snmp_asn1_enc_s32t).
#
# demo/cve-2019-13223's predict_point is deliberately absent. Run with every
# check on, its first counterexample is an overflow for an `x` outside the
# segment, not the CVE's division by zero, and no record says whether a
# caller can pass that -- so there is no ground truth to grade against.
#
# Four of the real ones are read from benchmarks/repro/, which holds the
# functions verbatim as they were when found: parson and lwIP have since
# fixed the UTF-8 and NetBIOS reads upstream, so a fresh clone verifies.
CASES = [
    # ---- not real: the inputs are ones no caller produces -----------------
    Case("lvandeve/lodepng", "lodepng/lodepng.cpp", "reverseBits", NOT_REAL,
         "callers pass FIRSTBITS=9 or a code length bounded by maxbitlen<=15",
         "benchmarks/eval_triage.py"),
    Case("nothings/stb", "stb/stb_image_write.h", "stbiw__zlib_bitrev", NOT_REAL,
         "only ever called with the constants 5, 7, 8 and 9",
         "benchmarks/eval_triage.py", defines=["STB_IMAGE_WRITE_IMPLEMENTATION"]),
    Case("richgel999/miniz", "miniz/miniz_tdef.c", "tdefl_record_match", NOT_REAL,
         "miniz's own MZ_ASSERT; every call site normalises the value first",
         f"{FIELD} #7", includes=["stubs"]),
    Case("kgabis/parson", "parson/parson.c", "json_object_init", NOT_REAL,
         "static; the one caller passes a capacity that needs a 2^64-byte "
         "allocation to have succeeded", f"{FIELD} #14"),
    Case("kgabis/parson", "parson/parson.c", "json_serialize_string", NOT_REAL,
         "output buffer with no length parameter; callers size it first",
         f"{FIELD} #15"),
    Case("codeplea/tinyexpr", "tinyexpr/tinyexpr.c", "npr", NOT_REAL,
         "NaN is tinyexpr's error value, defined IEEE behaviour",
         f"{FIELD} #13"),
    Case("DaveGamble/cJSON", "cJSON/cJSON.c", "parse_string", NOT_REAL,
         "its only caller checks can_access_at_index(input_buffer, 0) first",
         f"{FIELD} #17"),
    Case("DaveGamble/cJSON", "cJSON/cJSON.c", "update_offset", NOT_REAL,
         "callers have just written a terminated chunk at the offset",
         f"{FIELD} #18"),
    Case("DaveGamble/cJSON", "cJSON/cJSON.c", "print_number", NOT_REAL,
         "sscanf is not modelled, so `test` stays unconstrained",
         f"{FIELD} #19"),
    Case("DaveGamble/cJSON", "cJSON/cJSON_Utils.c", "detach_item_from_array", NOT_REAL,
         "walks ->prev, never null in a list the library builds",
         f"{FIELD} (struct graph class)"),
    Case("DaveGamble/cJSON", "cJSON/cJSON_Utils.c", "insert_item_in_array", NOT_REAL,
         "walks ->prev, never null in a list the library builds",
         f"{FIELD} (struct graph class)"),
    # ---- real: confirmed under a sanitizer --------------------------------
    Case("gfabbretti8/veripp", f"{REPRO}/netbios_decode.c", "netbiosns_name_decode", REAL,
         "the read is bounded by nothing; the bytes are a UDP datagram's",
         "benchmarks/LWIP_NETBIOS_NAME_DECODE.md", unterminated=True),
    Case("gfabbretti8/veripp", f"{REPRO}/ppp_slprintf_v.c", "vslprintf_v", REAL,
         "%.*v calls strlen on a counted, unterminated field",
         "benchmarks/LWIP_PPP_SLPRINTF_V.md", unterminated=True),
    Case("gfabbretti8/veripp", f"{REPRO}/tinyusb_utf16.c", "_convert_utf16le_to_utf8", REAL,
         "the destination size is passed and discarded; UTF-8 can be 1.5x "
         "the UTF-16 it is converted from, in place",
         "benchmarks/TINYUSB_UTF16_DESCRIPTOR.md"),
    Case("gfabbretti8/veripp", f"{REPRO}/tinyusb_utf16.c", "print_utf16", REAL,
         "bLength comes from the device and sets how far the buffer is read",
         "benchmarks/TINYUSB_UTF16_DESCRIPTOR.md"),
    Case("gfabbretti8/veripp", "veripp/examples/off_by_one.cpp", "sum_array", REAL,
         "i <= n reads a[n] for every n", "examples/off_by_one.cpp"),
]

CLASSIFY_MARK = "You triage counterexamples"
EXPLAIN_MARK = "Explain this model-checker counterexample"
PROPOSE_MARK = "Propose ONE C++ boolean expression"


class Recorder(PromptedLLM):
    """Answers nothing useful and keeps every question."""

    PROVIDER, MODEL = "recorder", "recorder"

    def __init__(self) -> None:
        self.asked: dict[str, dict[str, str]] = {}

    def _ask(self, system: str, user: str, max_tokens: int = 16000) -> str:
        if PROPOSE_MARK in system:
            self.asked.setdefault("precondition", {"system": system, "user": user})
            return "NONE"
        if EXPLAIN_MARK in system:
            return "(not asked)"
        if CLASSIFY_MARK not in system:
            # A request to repair the file or add invariants: the run never
            # got as far as a counterexample, so this is not a triage case.
            return ""
        self.asked.setdefault("classify", {"system": system, "user": user})
        # Not real_bug, so that the loop goes on to ask for a precondition
        # and that prompt is recorded too.
        return "missing_assumption"


class Replay(PromptedLLM):
    """Serves the answers saved for one case."""

    PROVIDER = "replay"

    def __init__(self, answers: Path, case: str, model: str) -> None:
        self.answers, self.case, self.MODEL = answers, case, model

    def _read(self, kind: str) -> str:
        path = self.answers / f"{self.case}.{kind}.txt"
        if not path.is_file():
            raise LLMError(f"no saved answer: {path.name}")
        return path.read_text(encoding="utf-8")

    def _ask(self, system: str, user: str, max_tokens: int = 16000) -> str:
        if PROPOSE_MARK in system:
            try:
                return self._read("precondition")
            except LLMError:
                return "NONE"  # the answerer proposed nothing
        if EXPLAIN_MARK in system:
            return "(not asked)"
        if CLASSIFY_MARK not in system:
            return ""
        return self._read("classify")


def run(case: Case, corpus: Path, llm: PromptedLLM, timeout: int):
    source = corpus / case.rel
    options = HarnessOptions(terminated_strings=not case.unterminated)
    target = TargetInfo(source=source, function=case.function, options=options)
    harness = generate(source, case.function, target.options)
    path = harness.write(scratch_dir())
    config = VerifyConfig(
        timeout_s=timeout,
        include_dirs=[contracts_include_dir(), source.parent]
        + [corpus / inc for inc in case.includes],
        defines=case.defines,
    )
    return verify_with_agent(
        path, config, llm=llm, budget=Budget(),
        assumptions=harness.assumptions, harness=path, target=target,
    )


def ensure_corpus(corpus: Path) -> None:
    for repo in sorted({c.repo for c in CASES}):
        dest = corpus / repo.split("/")[1]
        if dest.exists():
            continue
        if repo == "gfabbretti8/veripp":
            dest.symlink_to(REPO)
            continue
    stubs = corpus / "stubs"   # miniz includes a header its build generates
    stubs.mkdir(exist_ok=True)
    (stubs / "miniz_export.h").write_text("#define MINIZ_EXPORT\n", encoding="utf-8")
    for repo in sorted({c.repo for c in CASES}):
        dest = corpus / repo.split("/")[1]
        if dest.exists() or repo == "gfabbretti8/veripp":
            continue
        subprocess.run(["git", "clone", "-q", "--depth", "1",
                        f"https://github.com/{repo}.git", str(dest)], check=True)


def capture(corpus: Path, out: Path, timeout: int, only: str | None) -> int:
    prompts = out / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    index = []
    for case in CASES:
        if only and only not in case.name:
            continue
        recorder = Recorder()
        try:
            report = run(case, corpus, recorder, timeout)
            status = "asked" if "classify" in recorder.asked else (
                f"not asked ({report.status if hasattr(report, 'status') else 'no triage'})"
            )
            diagnosis = getattr(report, "diagnosis", None)
            if "classify" not in recorder.asked and diagnosis is not None:
                status = f"not asked (decided mechanically: {diagnosis.kind})"
        except Exception as exc:  # a refusal, a frontend error: say so, go on
            status = f"not asked ({type(exc).__name__}: {str(exc)[:90]})"
        print(f"{case.name:44} {status}", flush=True)
        if "classify" in recorder.asked:
            (prompts / f"{case.name}.json").write_text(
                json.dumps(recorder.asked, indent=1), encoding="utf-8")
            # The same prompts as plain text, for an answerer that reads
            # files rather than JSON: a person, or an agent.
            ask = recorder.asked["classify"]
            (prompts / f"{case.name}.classify.txt").write_text(
                f"=== SYSTEM PROMPT ===\n{ask['system']}\n\n"
                f"=== USER MESSAGE ===\n{ask['user']}\n", encoding="utf-8")
            if "precondition" in recorder.asked:
                (prompts / f"{case.name}.precondition.txt").write_text(
                    "=== SYSTEM PROMPT ===\n"
                    f"{recorder.asked['precondition']['system']}\n\n"
                    "=== USER MESSAGE ===\n(the same user message as in "
                    f"{case.name}.classify.txt)\n", encoding="utf-8")
            index.append(case.name)
    print(f"\n{len(index)} of {len(CASES)} cases reach the model")
    return 0


def grade(corpus: Path, out: Path, model: str, timeout: int) -> int:
    answers = out / "answers" / model
    names = sorted(p.stem for p in (out / "prompts").glob("*.json"))
    rows = []
    for case in CASES:
        if case.name not in names:
            continue
        report = run(case, corpus, Replay(answers, case.name, model), timeout)
        diagnosis = report.diagnosis
        if diagnosis is None or diagnosis.llm_error:
            rows.append(dict(case=case.name, truth=case.truth, kind="(no answer)",
                             correct=None, accepted=[], vacuous=False))
            continue
        said_real = diagnosis.kind == "real_bug"
        rows.append(dict(
            case=case.name, truth=case.truth, kind=diagnosis.kind,
            correct=said_real == (case.truth == REAL),
            proposed=diagnosis.proposed_precondition,
            accepted=list(report.accepted_preconditions),
            vacuous=bool(report.vacuous),
        ))
        print(f"{case.name:44} truth={case.truth:8} said={diagnosis.kind:18} "
              f"{'ok' if rows[-1]['correct'] else 'WRONG'}"
              + (f"  accepted: {rows[-1]['accepted']}" if rows[-1]["accepted"] else ""),
              flush=True)
    (out / "answers" / f"{model}.graded.json").write_text(
        json.dumps(rows, indent=1), encoding="utf-8")
    graded = [r for r in rows if r["correct"] is not None]
    real = [r for r in graded if r["truth"] == REAL]
    false = [r for r in graded if r["truth"] == NOT_REAL]
    print(f"\n{model}: {sum(r['correct'] for r in graded)}/{len(graded)} correct; "
          f"real bugs kept {sum(r['correct'] for r in real)}/{len(real)}; "
          f"false alarms dismissed {sum(r['correct'] for r in false)}/{len(false)}; "
          f"preconditions the solver accepted "
          f"{sum(bool(r['accepted']) for r in false)}/{len(false)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["capture", "grade"])
    ap.add_argument("corpus", type=Path, help="where the libraries are cloned")
    ap.add_argument("out", type=Path, help="where prompts and answers live")
    ap.add_argument("--model", help="grade: the directory under OUT/answers")
    ap.add_argument("--only", help="capture: only cases whose name contains this")
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()
    ensure_corpus(args.corpus)
    if args.command == "capture":
        return capture(args.corpus, args.out, args.timeout, args.only)
    if not args.model:
        ap.error("grade needs --model")
    return grade(args.corpus, args.out, args.model, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
