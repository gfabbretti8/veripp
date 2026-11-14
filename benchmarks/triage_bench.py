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
from veripp.harness import generate  # noqa: E402
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

    @property
    def name(self) -> str:
        return f"{Path(self.rel).stem}.{self.function}"


FIELD = "benchmarks/HUNT_FIELD_REPORT.md"
LWIP = ["lwip/src/include", "lwip/contrib/ports/unix/port/include",
        "lwip/contrib/ports/unix/lib"]

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
         f"{FIELD} #7"),
    Case("kgabis/parson", "parson/parson.c", "json_object_init", NOT_REAL,
         "static; the one caller passes a capacity that needs a 2^64-byte "
         "allocation to have succeeded", f"{FIELD} #14"),
    Case("kgabis/parson", "parson/parson.c", "json_serialize_string", NOT_REAL,
         "output buffer with no length parameter; callers size it first",
         f"{FIELD} #15"),
    Case("kgabis/parson", "parson/parson.c", "is_decimal", NOT_REAL,
         "a NUL planted inside a counted string no tokeniser produces",
         f"{FIELD} #12"),
    Case("codeplea/tinyexpr", "tinyexpr/tinyexpr.c", "find_builtin", NOT_REAL,
         "a NUL inside an identifier; the tokeniser's identifiers have none",
         f"{FIELD} #11"),
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
    Case("DaveGamble/cJSON", "cJSON/cJSON.c", "cJSON_DetachItemViaPointer", NOT_REAL,
         "item->prev is never null in cJSON's circular sibling lists",
         f"{FIELD} #16"),
    Case("DaveGamble/cJSON", "cJSON/cJSON.c", "cJSON_Minify", NOT_REAL,
         "the documented contract is a NUL-terminated string",
         "benchmarks/CORPUS.md (cJSON, triaged)"),
    Case("DaveGamble/cJSON", "cJSON/cJSON_Utils.c", "detach_item_from_array", NOT_REAL,
         "walks ->prev, never null in a list the library builds",
         f"{FIELD} (struct graph class)"),
    Case("DaveGamble/cJSON", "cJSON/cJSON_Utils.c", "insert_item_in_array", NOT_REAL,
         "walks ->prev, never null in a list the library builds",
         f"{FIELD} (struct graph class)"),
    Case("lwip-tcpip/lwip", "lwip/src/netif/ppp/mppe.c", "mppe_rekey", NOT_REAL,
         "mppe_init sets keylen to exactly 16 or 8, or never calls it",
         f"{FIELD} (internal function class)", includes=LWIP),
    Case("lwip-tcpip/lwip", "lwip/src/apps/snmp/snmp_asn1.c", "snmp_asn1_enc_s32t", NOT_REAL,
         "every caller passes the 1-4 that snmp_asn1_enc_s32t_cnt computes",
         f"{FIELD} (internal function class)", includes=LWIP),
    # ---- real: confirmed under a sanitizer --------------------------------
    Case("lwip-tcpip/lwip", "lwip/src/core/def.c", "lwip_strnstr", REAL,
         "reads buffer past n; httpd passes a non-terminated pbuf payload",
         "benchmarks/LWIP_STRING_HELPERS.md", includes=LWIP),
    Case("lwip-tcpip/lwip", "lwip/src/core/def.c", "lwip_strnistr", REAL,
         "same over-read as lwip_strnstr",
         "benchmarks/LWIP_STRING_HELPERS.md", includes=LWIP),
    Case("kgabis/parson", "parson/parson.c", "verify_utf8_sequence", REAL,
         "reads continuation bytes past the counted string; reachable from "
         "json_value_init_string_with_len", "benchmarks/PARSON_UTF8_OVERREAD.md"),
    Case("kgabis/parson", "parson/parson.c", "is_valid_utf8", REAL,
         "passes verify_utf8_sequence a sequence the length does not cover",
         "benchmarks/PARSON_UTF8_OVERREAD.md"),
    Case("lwip-tcpip/lwip", "lwip/src/apps/netbiosns/netbiosns.c", "netbiosns_name_decode", REAL,
         "the read is bounded by nothing; the bytes are a UDP datagram's",
         "benchmarks/LWIP_NETBIOS_NAME_DECODE.md", includes=LWIP),
    Case("lwip-tcpip/lwip", "lwip/src/netif/ppp/utils.c", "ppp_vslprintf", REAL,
         "%.*v calls strlen on a counted field",
         "benchmarks/LWIP_PPP_SLPRINTF_V.md", includes=LWIP),
    Case("gfabbretti8/veripp", "veripp/examples/off_by_one.cpp", "sum_array", REAL,
         "i <= n reads a[n] for every n", "examples/off_by_one.cpp"),
    Case("gfabbretti8/veripp", "veripp/demo/cve-2019-13223/predict_point.c", "predict_point", REAL,
         "CVE-2019-13223: x1 == x0 divides by zero, from file data",
         "demo/cve-2019-13223/README.md"),
]

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
        return self._read("classify")


def run(case: Case, corpus: Path, llm: PromptedLLM, timeout: int):
    source = corpus / case.rel
    target = TargetInfo(source=source, function=case.function)
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
            index.append(case.name)
    (out / "cases.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"\n{len(index)} of {len(CASES)} cases reach the model")
    return 0


def grade(corpus: Path, out: Path, model: str, timeout: int) -> int:
    answers = out / "answers" / model
    names = json.loads((out / "cases.json").read_text(encoding="utf-8"))
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
