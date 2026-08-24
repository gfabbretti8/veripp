"""veripp command-line interface."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path

from .agent import AgentReport, Budget, verify_with_agent
from .compdb import CompDBError, entry_for, find_database
from .cppsig import SignatureError
from .esbmc import Outcome, VerifyConfig, check_soundness, find_esbmc
from .harness import (
    Harness,
    HarnessError,
    HarnessOptions,
    generate,
    generate_sequence,
)
from .llm import NullLLM, make_llm
from .paths import contracts_include_dir, scratch_dir
from .scan import scan
from .triage import TargetInfo

_DEFAULT_STD = "c++17"

EXIT_VERIFIED = 0
EXIT_COUNTEREXAMPLE = 1
EXIT_USAGE = 2
EXIT_INCONCLUSIVE = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="veripp", description="AI-operated formal verification for C++"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("verify", help="verify a self-contained C++ file")
    _add_common_args(v)
    v.add_argument("--no-llm", action="store_true", help="run the plain verifier pipeline offline")
    v.add_argument(
        "--model",
        metavar="PROVIDER:MODEL",
        help="LLM used for triage, e.g. anthropic:claude-opus-5, "
        "openai:gpt-4o-mini, gemini:gemini-2.0-flash, groq:llama-3.3-70b-versatile, "
        "ollama:llama3.1 (local, no account). Defaults to $VERIPP_LLM_MODEL.",
    )
    v.add_argument(
        "--llm-base-url",
        metavar="URL",
        help="OpenAI-compatible endpoint for any provider not listed above "
        "(self-hosted gateways, vLLM, Azure). Defaults to $VERIPP_LLM_BASE_URL.",
    )
    v.add_argument("--json", action="store_true", help="machine-readable output")
    v.add_argument("--keep-harness", action="store_true", help="print where the harness was written")

    h = sub.add_parser("harness", help="print the generated harness without verifying")
    _add_common_args(h, require_function=True)

    s = sub.add_parser(
        "scan",
        help="verify every function in a file that veripp can harness",
    )
    _add_common_args(s)
    s.add_argument("--jobs", "-j", type=int, default=4, help="parallel verifications")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.add_argument("--quiet", "-q", action="store_true", help="summary only, no progress")
    s.add_argument(
        "--escalations",
        type=int,
        default=1,
        help="how many times to widen the unwind bound when a function runs "
        "out of it (0 disables; each round costs another solver run)",
    )

    d = sub.add_parser("doctor", help="check that dependencies are available")
    d.add_argument(
        "--allow-unsound",
        action="store_true",
        help="report soundness holes but exit 0 anyway (for CI pinned to a "
        "release with a known, accepted hole)",
    )

    args = parser.parse_args(argv)

    if args.command == "doctor":
        return _doctor(allow_unsound=args.allow_unsound)
    if not args.source.exists():
        print(f"error: {args.source} not found", file=sys.stderr)
        return EXIT_USAGE

    if args.command == "scan":
        return _scan(args)

    if args.command == "harness":
        try:
            print(_build_harness(args).code, end="")
        except (HarnessError, SignatureError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE
        return EXIT_VERIFIED

    return _verify(args)


def _add_common_args(p: argparse.ArgumentParser, require_function: bool = False) -> None:
    p.add_argument("source", type=Path)
    what = p.add_argument_group("what to verify")
    bounds = p.add_argument_group(
        "bounds", "every result states the bounds it was obtained under"
    )
    build = p.add_argument_group(
        "build configuration", "usually taken from compile_commands.json"
    )
    target = what.add_mutually_exclusive_group(required=require_function)
    target.add_argument(
        "--function",
        help="target function; veripp generates a harness for it. Overloads "
        "are picked by parameter types: --function 'f(int, unsigned)'. "
        "(omit to verify the file's own main)",
    )
    target.add_argument(
        "--class",
        dest="cls",
        help="target class; veripp drives a nondeterministic sequence of its "
        "public methods, so states built up across calls are explored",
    )
    what.add_argument(
        "--max-calls",
        type=int,
        default=HarnessOptions.max_calls,
        help="length of the generated call sequence for --class",
    )
    what.add_argument(
        "--assert",
        dest="assertions",
        action="append",
        default=[],
        metavar="EXPR",
        help="property checked after every call in a --class sequence; the "
        "object under test is named `veripp_obj`. Repeatable.",
    )
    bounds.add_argument("--unwind", type=int, default=8)
    bounds.add_argument("--timeout", type=int, default=120, help="per-attempt timeout (s)")
    build.add_argument("--std", default=_DEFAULT_STD)
    bounds.add_argument(
        "--max-array-len",
        type=int,
        default=HarnessOptions.max_array_len,
        help="harness bound on generated buffer lengths",
    )
    build.add_argument(
        "--compile-commands",
        type=Path,
        metavar="PATH",
        help="clang compilation database (or a directory holding one). "
        "Include paths, defines and the language standard for the target file "
        "are taken from it. Auto-discovered near the source if not given; "
        "--no-compile-commands disables that.",
    )
    build.add_argument(
        "--no-compile-commands",
        action="store_true",
        help="do not look for a compilation database",
    )
    build.add_argument(
        "--link",
        action="append",
        type=Path,
        default=[],
        metavar="SOURCE",
        help="also compile this translation unit. Needed when the target "
        "calls a function defined elsewhere: an unlinked callee is assumed to "
        "have no side effects, which is unsound. Repeatable.",
    )
    build.add_argument("-I", "--include", action="append", type=Path, default=[])
    build.add_argument("-D", "--define", action="append", default=[], help="preprocessor macro")
    build.add_argument(
        "--include-file",
        action="append",
        default=[],
        metavar="HEADER",
        help="force-include a header before the source (e.g. a libc/typedef shim "
        "for a symbol esbmclibc lacks); repeatable",
    )
    bounds.add_argument(
        "--max-struct-depth",
        type=int,
        default=HarnessOptions.max_struct_depth,
        help="how far to follow pointer fields when building an object; "
        "beyond it they are null, which is reported as an assumption",
    )
    bounds.add_argument(
        "--no-initializers",
        action="store_true",
        help="fill object parameters field by field instead of calling the "
        "library's own initialiser. Broader, but admits field combinations "
        "the type's invariants forbid, so expect failures no caller can cause.",
    )
    bounds.add_argument(
        "--no-overflow-check",
        action="store_true",
        help="disable arithmetic overflow checking (isolate other properties)",
    )
    bounds.add_argument(
        "--assume",
        action="append",
        default=[],
        metavar="EXPR",
        help="add a precondition over the target's parameters (e.g. --assume "
        "'x1 != x0'); this is what LLM triage proposes automatically, exposed "
        "for manual use. Repeatable. Requires --function.",
    )


def _harness_options(args) -> HarnessOptions:
    return HarnessOptions(
        max_array_len=args.max_array_len,
        max_calls=getattr(args, "max_calls", HarnessOptions.max_calls),
        max_struct_depth=getattr(
            args, "max_struct_depth", HarnessOptions.max_struct_depth
        ),
        include_dirs=_include_dirs(args),
        link_sources=[s.resolve() for s in getattr(args, "link", [])],
        use_initializers=not getattr(args, "no_initializers", False),
    )


def _build_harness(args) -> Harness:
    if getattr(args, "cls", None):
        return generate_sequence(
            args.source,
            args.cls,
            _harness_options(args),
            assertions=list(getattr(args, "assertions", []) or []),
        )
    return generate(
        args.source,
        args.function,
        _harness_options(args),
        extra_preconditions=list(getattr(args, "assume", []) or []),
    )


def _unconfigured_build_hint(source: Path, include_dirs: list[Path]) -> str | None:
    """Spot a header the build system generates but has not generated yet.

    A project that ships `config.h.in` and no `config.h` has simply not been
    configured, and the compiler says so as `use of undeclared identifier
    YAML_VERSION_STRING` -- true, and no help at all.
    """
    # Matched on the raw text: scrub() blanks string literals, which erases
    # the filename in `#include "config.h"`. (Second time that has bitten --
    # the same mistake is commented in harness._with_local_includes.)
    pattern = r'^[ \t]*#[ \t]*include[ \t]*"([^"]+)"'
    search = [source.parent, *include_dirs]

    def includes_of(path: Path) -> list[str]:
        try:
            return re.findall(pattern, path.read_text(errors="replace"), re.M)
        except OSError:
            return []

    # The missing header is usually one level in: a .c includes the project's
    # private header, and that is what includes the generated config.
    names = list(includes_of(source))
    for name in list(names):
        found = next((d / name for d in search if (d / name).is_file()), None)
        if found is not None:
            names += includes_of(found)

    missing: list[str] = []
    for name in dict.fromkeys(names):
        if any((d / name).is_file() for d in search):
            continue
        template = next(
            (
                d / f"{name}{ext}"
                for d in [*search, *source.parents[:3],
                          *(q / "cmake" for q in source.parents[:3])]
                for ext in (".in", ".cmake")
                if (d / f"{name}{ext}").is_file()
            ),
            None,
        )
        if template is not None:
            missing.append(f"{name} (template at {template})")
    if not missing:
        return None
    return (
        "note: this project has not been configured -- "
        + ", ".join(missing)
        + ".\n  Run its build once (cmake/configure) so the generated headers "
        "exist, then point veripp at the resulting compile_commands.json."
    )


def _suggest_targets(args, wanted: str) -> None:
    """Point at the names that do exist, rather than only refusing."""
    import difflib

    from .cppsig import function_definitions

    try:
        names = [n for n in function_definitions(args.source.read_text(errors="replace"))
                 if n != "main"]
    except OSError:
        return
    if not names:
        return
    base = wanted.split("(")[0]
    close = difflib.get_close_matches(base, names, n=3, cutoff=0.6)
    if close:
        print(f"  did you mean: {', '.join(close)}?", file=sys.stderr)
    else:
        shown = ", ".join(names[:6]) + ("..." if len(names) > 6 else "")
        print(f"  this file defines: {shown}", file=sys.stderr)
    print(f"  or scan them all:  veripp scan {args.source}", file=sys.stderr)


def _scan(args) -> int:
    config = _config_for(args)
    options = _harness_options(args)
    seen: list[str] = []

    def progress(done: int, total: int, result) -> None:
        if args.quiet or args.json:
            return
        mark = {"verified": "PROVED", "counterexample": "COUNTEREX",
                "refused": "skip"}.get(result.outcome, result.outcome)
        if result.artifact:
            mark = "artifact"
        print(f"[{done:4d}/{total}] {mark:>10}  {result.name}", file=sys.stderr)
        seen.append(result.name)

    report = scan(args.source, config, options, jobs=args.jobs, progress=progress,
                  escalations=args.escalations)

    if args.json:
        print(json.dumps({
            "source": str(report.source),
            "candidates": report.candidates,
            "proved": [r.name for r in report.proved],
            "counterexamples": [
                {"function": r.name, "signature": r.signature, "property": r.detail,
                 "assumptions": r.assumptions, "stubbed_calls": r.stubbed_calls}
                for r in report.counterexamples
            ],
            "artifacts": [
                {"function": r.name, "property": r.detail, "why": r.artifact}
                for r in report.artifacts
            ],
            "inconclusive": [{"function": r.name, "outcome": r.outcome} for r in report.inconclusive],
            "not_harnessable": report.refusal_reasons(),
        }, indent=2, default=str))
    else:
        print(report.summary())
    return EXIT_VERIFIED if not report.counterexamples else EXIT_COUNTEREXAMPLE


def _config_for(args) -> VerifyConfig:
    extra_args: list[str] = []
    for header in args.include_file:
        extra_args += ["--include-file", str(header)]
    std = args.std
    defines = list(args.define)
    include_dirs = _include_dirs(args)
    entry = _compile_entry(args)
    if entry is not None:
        defines = entry.defines + defines
        extra_args += [a for a in entry.esbmc_args() if a == "--include-file"] and []
        for header in entry.force_includes:
            extra_args += ["--include-file", str(header)]
        if entry.std and args.std == _DEFAULT_STD and _is_cxx_std(entry.std):
            std = entry.std
    return VerifyConfig(
        unwind=args.unwind,
        timeout_s=args.timeout,
        cpp_std=std,
        include_dirs=include_dirs,
        defines=defines,
        link_sources=[s.resolve() for s in getattr(args, "link", [])],
        overflow_check=not args.no_overflow_check,
        extra_args=extra_args,
    )


def _verify(args) -> int:
    harness: Harness | None = None
    target = args.source
    if args.function or getattr(args, "cls", None):
        try:
            harness = _build_harness(args)
        except (HarnessError, SignatureError) as exc:
            what = args.function or args.cls
            print(f"error: cannot harness `{what}`: {exc}", file=sys.stderr)
            _suggest_targets(args, what)
            return EXIT_USAGE
        target = harness.write(scratch_dir())

    extra_args: list[str] = []
    for header in args.include_file:
        extra_args += ["--include-file", str(header)]

    std = args.std
    defines = list(args.define)
    include_dirs = _include_dirs(args)
    entry = _compile_entry(args)
    if entry is not None:
        include_dirs = list(entry.include_dirs) + include_dirs
        defines = entry.defines + defines
        extra_args += entry.esbmc_args()[len(entry.include_dirs) * 2 + len(entry.defines) * 2 :]
        if entry.std and args.std == _DEFAULT_STD and _is_cxx_std(entry.std):
            std = entry.std

    config = VerifyConfig(
        unwind=args.unwind,
        timeout_s=args.timeout,
        cpp_std=std,
        include_dirs=include_dirs,
        defines=defines,
        link_sources=[s.resolve() for s in args.link],
        overflow_check=not args.no_overflow_check,
        extra_args=extra_args,
    )
    llm, deferred_note = (NullLLM(), None) if args.no_llm else _make_llm(args)
    target_info = (
        TargetInfo(
            source=args.source,
            function=args.function,
            options=_harness_options(args),
        )
        if harness and args.function
        else None
    )
    report = verify_with_agent(
        target,
        config,
        llm=llm,
        budget=Budget(),
        assumptions=harness.assumptions if harness else [],
        harness=target if harness else None,
        target=target_info,
    )
    if harness and getattr(args, "assume", None):
        report.accepted_preconditions = list(args.assume) + report.accepted_preconditions

    if report.final.outcome is Outcome.PARSE_ERROR:
        hint = _unconfigured_build_hint(args.source, _include_dirs(args))
        if hint:
            print(hint, file=sys.stderr)

    if deferred_note and report.final.outcome is Outcome.COUNTEREXAMPLE:
        print(f"note: {deferred_note}", file=sys.stderr)

    if report.final.outcome is Outcome.VERIFIED:
        try:
            report.unsound_probes = [
                name for name, ok in check_soundness().items() if not ok
            ]
        except RuntimeError:
            pass

    if args.json:
        print(json.dumps(_payload(report, harness), indent=2, default=str))
    else:
        print(report.summary())
        if harness and not args.keep_harness:
            print(f"(harness kept at {target})")

    return _exit_code(report)


def _is_cxx_std(std: str) -> bool:
    """veripp's harness is always C++ (it needs `extern "C"` and references),
    and it #includes the target. A C project's -std=c11 would be rejected on a
    .cpp file, so the build system's C standard is deliberately not forwarded.
    """
    return "++" in std or std.startswith(("gnu++", "c++"))


def _compile_entry(args, quiet: bool = False):
    """Flags for the target file from a compilation database, if there is one."""
    if getattr(args, "no_compile_commands", False):
        return None
    database = args.compile_commands
    if database is not None and database.is_dir():
        database = database / "compile_commands.json"
    if database is None:
        database = find_database(args.source)
        if database is None:
            return None
    elif not database.is_file():
        print(f"error: {database} not found", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    try:
        entry = entry_for(database, args.source)
    except CompDBError as exc:
        # Auto-discovery must never break a run that would otherwise work.
        if args.compile_commands is not None:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(EXIT_USAGE) from exc
        if not quiet:
            print(f"note: {exc}", file=sys.stderr)
        return None
    if quiet:
        return entry
    print(
        f"note: using {database} "
        f"({len(entry.include_dirs)} include dirs, {len(entry.defines)} defines"
        + (f", -std={entry.std}" if entry.std else "")
        + ")",
        file=sys.stderr,
    )
    return entry


def _include_dirs(args) -> list[Path]:
    dirs: list[Path] = []
    entry = _compile_entry(args, quiet=True)
    if entry is not None:
        dirs += entry.include_dirs
    dirs += list(args.include)
    bundled = contracts_include_dir()
    if bundled is not None and bundled not in dirs:
        dirs.append(bundled)
    parent = args.source.resolve().parent
    if parent not in dirs:
        dirs.append(parent)
    return dirs


def _payload(report: AgentReport, harness: Harness | None) -> dict:
    prop = report.final.violated_property
    return {
        "outcome": report.final.outcome.value,
        "bounded": not report.final.config.k_induction,
        "vacuous": report.vacuous,
        "config": asdict(report.final.config),
        "assumptions": report.assumptions,
        "accepted_preconditions": report.accepted_preconditions,
        "unsound_probes": report.unsound_probes,
        "harness": str(report.harness) if report.harness else None,
        "stubbed_calls": report.final.stubbed_calls,
        "function": harness.signature.qualified_name if harness else None,
        "sequence": bool(harness and harness.class_info),
        "violated_property": asdict(prop) if prop else None,
        "trace": [asdict(step) for step in report.final.trace],
        "diagnosis": asdict(report.diagnosis) if report.diagnosis else None,
        "narrative": report.narrative,
        "error": report.final.error,
        "attempts": len(report.attempts),
        "duration_s": report.final.duration_s,
    }


def _exit_code(report: AgentReport) -> int:
    outcome = report.final.outcome
    if report.vacuous:
        return EXIT_INCONCLUSIVE  # a vacuous proof is not a pass
    if outcome is Outcome.VERIFIED:
        return EXIT_VERIFIED
    if outcome is Outcome.COUNTEREXAMPLE:
        return EXIT_COUNTEREXAMPLE
    return EXIT_INCONCLUSIVE


def _make_llm(args) -> tuple[object, str | None]:
    """The triage client, plus a note to show only if it turns out to matter.

    Telling someone their counterexamples will not be triaged is useless when
    the answer is "verified" -- it is advice about a problem they do not have.
    The note is held back and printed only when a counterexample appears.
    """
    try:
        llm = make_llm(getattr(args, "model", None), getattr(args, "llm_base_url", None))
    except RuntimeError as exc:
        return NullLLM(), str(exc)
    print(f"note: triage via {llm.PROVIDER}", file=sys.stderr)
    return llm, None


def _esbmc_install_hint() -> str:
    """The exact command for this machine, not a link to go read."""
    import platform

    if platform.system() == "Darwin":
        return "brew install --HEAD esbmc"
    return (
        "curl -fsSL -o /tmp/esbmc.zip "
        "https://github.com/esbmc/esbmc/releases/download/weekly/esbmc-linux.zip "
        "&& unzip -q /tmp/esbmc.zip -d ~/.local/esbmc "
        "&& chmod +x ~/.local/esbmc/*/bin/esbmc"
    )


def _doctor(allow_unsound: bool = False) -> int:
    esbmc = find_esbmc()
    if esbmc:
        print(f"esbmc: {esbmc}")
    else:
        print("esbmc: NOT FOUND — veripp cannot verify anything without it.")
        print(f"  install it with:  {_esbmc_install_hint()}")
    if esbmc:
        import subprocess

        version = subprocess.run([esbmc, "--version"], capture_output=True, text=True)
        print(f"  {version.stdout.strip() or version.stderr.strip()}")
    unsound: list[str] = []
    if esbmc:
        print("soundness self-check (known-failing programs must be rejected):")
        try:
            for name, ok in check_soundness(esbmc).items():
                print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
                if not ok:
                    unsound.append(name)
        except RuntimeError as exc:
            print(f"  could not run: {exc}")
    include = contracts_include_dir()
    print(f"contracts header: {include / 'veripp/contracts.hpp' if include else 'NOT FOUND'}")
    import os

    from .llm import PROVIDERS

    configured = []
    for name, entry in sorted(PROVIDERS.items()):
        env = entry.get("api_key_env", "ANTHROPIC_API_KEY")
        if os.environ.get(env):
            configured.append(f"{name} (${env})")
    if os.environ.get("VERIPP_LLM_BASE_URL"):
        configured.append("custom ($VERIPP_LLM_BASE_URL)")
    print("llm providers with credentials: " + (", ".join(configured) or "none"))
    print("  any OpenAI-compatible endpoint works: --model provider:model "
          "[--llm-base-url URL]")
    if unsound and not allow_unsound:
        print(
            "\nWARNING: this esbmc silently misses "
            + ", ".join(unsound)
            + ".\n'verified' results covering that pattern are NOT trustworthy "
            "(esbmc/esbmc#6508 is fixed upstream but in no release yet).\n"
            f"  upgrade with:  {_esbmc_install_hint()}",
            file=sys.stderr,
        )
        return EXIT_INCONCLUSIVE
    if not esbmc:
        return EXIT_USAGE

    print("\nready. try:")
    print("  veripp verify examples/off_by_one.cpp --function sum_array   # finds a bug")
    print("  veripp scan   examples/ring_buffer.cpp                       # a whole file")
    if not unsound:
        print("  ./demo/cve-2019-13223/run.sh                                 # a real CVE")
    return EXIT_VERIFIED


if __name__ == "__main__":
    raise SystemExit(main())
