"""veripp command-line interface."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import NoReturn

from . import __version__, term
from .cache import DEFAULT_DIR as DEFAULT_CACHE_DIR
from .baseline import (
    DEFAULT_NAME as DEFAULT_BASELINE,
    Baseline,
    BaselineError,
    key_for,
)
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


class _Parser(argparse.ArgumentParser):
    """Argparse, but it behaves like a CLI people enjoy using.

    Two changes, both borrowed from tools that get this right (git, cargo, gh):
    a mistyped subcommand suggests the one you meant instead of listing all of
    them, and an error points at `--help` rather than dumping the full usage
    block, which is the least readable moment to show someone thirty flags.
    """

    def error(self, message: str) -> NoReturn:
        import difflib

        match = re.search(r"invalid choice: '([^']+)' \(choose from (.+)\)", message)
        if match:
            typo = match.group(1)
            choices = re.findall(r"'([^']+)'", match.group(2))
            close = difflib.get_close_matches(typo, choices, n=1, cutoff=0.5)
            hint = f"\n\nDid you mean:  {self.prog} {close[0]}" if close else ""
            sys.stderr.write(
                f"{self.prog}: unknown command '{typo}'{hint}\n"
                f"\nCommands: {', '.join(choices)}\n"
                f"Run '{self.prog} --help' for the full list.\n"
            )
            raise SystemExit(EXIT_USAGE)

        sys.stderr.write(f"{self.prog}: {message}\n")
        sys.stderr.write(f"Run '{self.prog} --help' to see the options.\n")
        raise SystemExit(EXIT_USAGE)


OVERVIEW = """\
veripp proves C/C++ functions free of overflow, out-of-bounds access, null
dereference and division by zero -- or hands you an input that breaks them.
You do not write the harness; veripp generates it from the signature.

  veripp doctor                       is the checker present, and is it sound?
  veripp scan   src/parser.c          every function in a file
  veripp verify src/parser.c --function parse_header
  veripp harness src/parser.c --function parse_header   see what it generated

Exit codes: 0 verified   1 counterexample   2 usage   3 inconclusive

Full options:  veripp <command> --help
"""


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(
        prog="veripp",
        description="AI-operated formal verification for C and C++",
        epilog=OVERVIEW,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"veripp {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=False, metavar="<command>")

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
    v.add_argument(
        "--json-out",
        metavar="PATH",
        help="also write the JSON report here, keeping the readable output "
             "on stdout (so CI can have both without verifying twice)",
    )
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
    s.add_argument(
        "--cache", type=Path, default=None, metavar="DIR",
        help=f"reuse verdicts for files that have not changed (default: "
             f"{DEFAULT_CACHE_DIR}; --no-cache to disable). The key covers the "
             "file, its local headers, linked sources, the bounds and the "
             "checker version, so a stale verdict cannot be served",
    )
    s.add_argument("--no-cache", action="store_true",
                   help="verify everything, ignoring any cached verdicts")
    s.add_argument(
        "--only", action="append", default=[], metavar="GLOB",
        help="verify only functions matching this glob (repeatable): "
             "--only 'parse_*' --only '*_decode'",
    )
    s.add_argument(
        "--sarif", type=Path, default=None, metavar="PATH",
        help="write findings as SARIF, for GitHub code scanning "
             "(baselined findings are marked suppressed, not dropped)",
    )
    s.add_argument(
        "--baseline", type=Path, default=None, metavar="PATH",
        help="findings recorded here are reported but do not fail the run; "
             f"write one with `veripp accept` (default name: {DEFAULT_BASELINE})",
    )
    s.add_argument(
        "--json-out",
        metavar="PATH",
        help="also write the JSON report here, keeping the readable output "
             "on stdout (so CI can have both without verifying twice)",
    )
    s.add_argument("--quiet", "-q", action="store_true", help="summary only, no progress")
    s.add_argument(
        "--escalations",
        type=int,
        default=1,
        help="how many times to widen the unwind bound when a function runs "
        "out of it (0 disables; each round costs another solver run)",
    )

    c = sub.add_parser(
        "completion",
        help="print a shell completion script",
        description="Print a completion script for your shell.\n\n"
                    "  bash:  eval \"$(veripp completion bash)\"\n"
                    "  zsh:   eval \"$(veripp completion zsh)\"\n"
                    "  fish:  veripp completion fish | source\n\n"
                    "Add the line to your shell's rc file to keep it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    c.add_argument("shell", choices=("bash", "zsh", "fish"))

    a = sub.add_parser(
        "accept",
        help="record current findings as known, so CI fails only on new ones",
        description=(
            "Scan and write the findings to a baseline file.\n\n"
            "Pointed at an existing codebase a verifier reports everything at "
            "once, and a check that goes red on day one is removed on day two. "
            "Accept what is already there, then `veripp scan --baseline` fails "
            "only on what appears afterwards.\n\n"
            "The file is JSON and meant to be reviewed: each entry is a risk "
            "someone decided to carry."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # `source` comes from _add_common_args below, which every scanning
    # subcommand shares.
    a.add_argument("--baseline", type=Path, default=None,
                   help=f"where to write it (default: {DEFAULT_BASELINE})")
    a.add_argument("--reason", default="",
                   help="why these are being accepted; recorded on every entry")
    _add_common_args(a)
    a.add_argument("--jobs", type=int, default=4, help="parallel verifications")
    a.add_argument("--escalations", type=int, default=1,
                   help="extra attempts with larger bounds")
    a.add_argument("--quiet", action="store_true", help="only the summary")
    a.add_argument("--json", action="store_true", help="machine-readable output")
    a.add_argument("--json-out", metavar="PATH", help=argparse.SUPPRESS)

    d = sub.add_parser("doctor", help="check that dependencies are available")
    d.add_argument(
        "--allow-unsound",
        action="store_true",
        help="report soundness holes but exit 0 anyway (for CI pinned to a "
        "release with a known, accepted hole)",
    )

    args = parser.parse_args(argv)

    # `veripp` on its own is someone finding out what this is. Show them,
    # and exit 0 -- they did not do anything wrong.
    if args.command is None:
        print(OVERVIEW, end="")
        return 0

    if args.command == "completion":
        print(_completion_script(args.shell, sub))
        return 0
    if args.command == "doctor":
        return _doctor(allow_unsound=args.allow_unsound)
    if not args.source.exists():
        print(f"error: {args.source} not found", file=sys.stderr)
        hint = _missing_source_hint()
        if hint:
            print(hint, file=sys.stderr)
        return EXIT_USAGE

    # Pointing at a directory is an ordinary slip -- `veripp scan .` reads as
    # though it should work. It used to reach read_text() and die with a
    # traceback, which is never an acceptable answer to a plausible mistake.
    # `scan` takes a directory; verify and harness target one function in one
    # file, so a directory there is still a mistake.
    if args.source.is_dir() and args.command != "scan":
        print(f"error: {args.source} is a directory; `veripp {args.command}` "
              "targets one function in one file", file=sys.stderr)
        print(f"  to scan the whole tree:  veripp scan {args.source}",
              file=sys.stderr)
        try:
            nearby = sorted(
                p.name for p in args.source.iterdir()
                if p.suffix in {".c", ".cc", ".cpp", ".cxx"}
            )[:5]
        except OSError:
            nearby = []
        if nearby:
            print(f"  try:  veripp {args.command} {args.source / nearby[0]}",
                  file=sys.stderr)
            if len(nearby) > 1:
                print(f"  this directory also has: {', '.join(nearby[1:])}",
                      file=sys.stderr)
        return EXIT_USAGE

    try:
        if args.source.is_file() and args.source.stat().st_size == 0:
            print(f"error: {args.source} is empty", file=sys.stderr)
            return EXIT_USAGE
    except OSError:
        pass

    if args.command == "accept":
        return _accept(args)

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
    from .cppsig import included_names

    search = [source.parent, *include_dirs]

    def includes_of(path: Path) -> list[str]:
        try:
            return included_names(path.read_text(errors="replace"))
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

    from .cppsig import find_class_ranges, function_definitions, scrub

    # Asking about a class and being shown a list of functions points at the
    # wrong kind of thing. find_class already suggests the right name; adding
    # function names underneath only muddies it.
    if getattr(args, "cls", None):
        try:
            classes = sorted({
                c.name for c in find_class_ranges(scrub(args.source.read_text(errors="replace")))
            })
        except OSError:
            return
        if classes:
            print(f"  classes in this file: {', '.join(classes[:6])}", file=sys.stderr)
        print(f"  or scan them all:  veripp scan {args.source}", file=sys.stderr)
        return

    try:
        names = [n for n in function_definitions(args.source.read_text(errors="replace"))
                 if n != "main"]
    except OSError:
        return
    if not names:
        # No C/C++ function definitions at all. If the suffix is not one we
        # recognise, that is almost certainly the actual problem.
        known = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inc"}
        if args.source.suffix and args.source.suffix not in known:
            print(f"  {args.source.name} does not look like a C or C++ source "
                  f"file ({args.source.suffix})", file=sys.stderr)
        else:
            print("  no C or C++ function definitions were found in this file",
                  file=sys.stderr)
        return
    base = wanted.split("(")[0]
    close = difflib.get_close_matches(base, names, n=3, cutoff=0.6)
    if close:
        print(f"  did you mean: {', '.join(close)}?", file=sys.stderr)
    else:
        shown = ", ".join(names[:6]) + ("..." if len(names) > 6 else "")
        print(f"  this file defines: {shown}", file=sys.stderr)
    print(f"  or scan them all:  veripp scan {args.source}", file=sys.stderr)


#: Extensions worth scanning. Headers are excluded by default: definitions
#: normally live in the source file, and scanning both doubles the work while
#: reporting the same functions twice.
SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx")

#: Directories that are almost never the code someone means to verify. Skipped
#: unless named directly, in the spirit of ripgrep ignoring .git.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "build", "_build", "out", "dist", "node_modules",
    "third_party", "vendor", "external", "deps", "subprojects", "cmake-build-debug",
    ".venv", "venv", "__pycache__",
}


def discover_sources(root: Path) -> list[Path]:
    """The C/C++ files under `root`, in a stable order.

    Deterministic because a scan that reports its findings in a different
    order each run is impossible to diff between commits.
    """
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.suffix not in SOURCE_SUFFIXES or not path.is_file():
            continue
        if any(part in SKIP_DIRS or part.startswith(".") for part in path.parts):
            continue
        found.append(path)
    return found


def _findings_from(reports) -> list:
    """Every counterexample across one or more scan reports, as baseline keys."""
    keys = []
    for report in reports:
        for result in report.counterexamples:
            keys.append(key_for(report.source, result.name, result.detail))
    return keys


def _cache_for(args):
    """The cache to use, or None."""
    if getattr(args, "no_cache", False):
        return None
    from .cache import Cache

    return Cache(Path(getattr(args, "cache", None) or DEFAULT_CACHE_DIR))


def _cache_key(args, source: Path, config, options) -> str:
    from .cache import esbmc_version, key_for
    from .cppsig import included_names
    from .esbmc import find_esbmc

    # Local headers and linked sources are inputs: a change in either can flip
    # this file's verdict without touching it.
    extra: list[Path] = [Path(p).resolve() for p in getattr(args, "link", [])]
    try:
        for name in included_names(source.read_text(errors="replace")):
            for directory in [source.parent, *getattr(args, "include", [])]:
                candidate = Path(directory) / name
                if candidate.is_file():
                    extra.append(candidate.resolve())
                    break
    except OSError:
        pass

    return key_for(
        source, config=config, options=options,
        veripp_version=__version__,
        checker_version=esbmc_version(find_esbmc()),
        extra_files=extra,
    )


def _report_from_cache(source: Path, payload: dict):
    """Rebuild a ScanReport from a cached entry."""
    from .scan import FunctionResult, ScanReport

    report = ScanReport(source=source, candidates=payload.get("candidates", 0))
    for item in payload.get("results", []):
        report.results.append(FunctionResult(**item))
    return report


def _cache_payload(report) -> dict:
    from dataclasses import asdict

    return {
        "candidates": report.candidates,
        "results": [asdict(r) for r in report.results],
    }


def _selected_names(args, source: Path) -> list[str] | None:
    """The functions --only asks for, or None for all of them.

    Returns an empty list when the patterns match nothing, which the caller
    reports rather than silently scanning everything -- a typo'd glob that
    quietly verified the whole file would be worse than an error.
    """
    patterns = getattr(args, "only", None)
    if not patterns:
        return None
    from fnmatch import fnmatch

    from .cppsig import function_definitions

    try:
        defined = [n for n in function_definitions(source.read_text(errors="replace"))
                   if n != "main"]
    except OSError:
        return None
    return [n for n in defined if any(fnmatch(n, p) for p in patterns)]


def _write_sarif(args, reports) -> None:
    """Emit SARIF if asked. Never fatal: a reporting format must not cost
    someone a verification result they already paid for."""
    destination = getattr(args, "sarif", None)
    if destination is None:
        return
    from . import sarif as sarif_mod

    baseline = _baseline_for(args)
    suppressed = set()
    if baseline is not None:
        for key in baseline.entries:
            suppressed.add((key.file, key.function, key.property))

    findings = []
    for report in reports:
        for result in report.counterexamples:
            findings.append({
                "file": result.file or str(report.source),
                "line": result.line, "column": result.column,
                "function": result.name, "property": result.detail,
                "cwes": result.cwes,
            })

    config = _config_for(args) if not args.source.is_dir() else None
    bounds = config.describe() if config is not None else ""
    try:
        sarif_mod.write(destination, sarif_mod.build(
            findings, root=Path.cwd(), version=__version__, bounds=bounds,
            suppressed=suppressed,
        ))
        if not args.quiet and not args.json:
            print(f"  sarif: {destination} ({len(findings)} result"
                  f"{'s' if len(findings) != 1 else ''})", file=sys.stderr)
    except OSError as exc:
        print(f"warning: could not write {destination}: {exc}", file=sys.stderr)


def _baseline_for(args):
    """The baseline to apply, or None. An explicitly named one that cannot be
    read is fatal; a default one that is simply absent is not."""
    named = getattr(args, "baseline", None)
    if named is None:
        return None
    try:
        return Baseline.load(named)
    except BaselineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE) from exc


def _apply_baseline(args, reports) -> tuple[int, dict]:
    """The exit code once accepted findings are discounted, plus JSON fields."""
    found = _findings_from(reports)
    baseline = _baseline_for(args)
    if baseline is None:
        return (EXIT_COUNTEREXAMPLE if found else EXIT_VERIFIED), {}

    new, known = baseline.split(found)
    stale = baseline.stale(found)
    return (EXIT_COUNTEREXAMPLE if new else EXIT_VERIFIED), {
        "baseline": str(args.baseline),
        "new_findings": [k.as_dict() for k in new],
        "known_findings": [k.as_dict() for k in known],
        "stale_baseline_entries": [k.as_dict() for k in stale],
    }


def _baseline_note(args, reports) -> str:
    """What the baseline changed about this run, in words."""
    baseline = _baseline_for(args)
    if baseline is None:
        return ""
    new, known = baseline.split(_findings_from(reports))
    stale = baseline.stale(_findings_from(reports))

    lines = ["", f"  baseline: {args.baseline}"]
    if known:
        lines.append(f"    {len(known)} known finding"
                     f"{'s' if len(known) != 1 else ''}, not failing this run")
    if new:
        lines.append(f"    {len(new)} NEW finding"
                     f"{'s' if len(new) != 1 else ''}:")
        for key in new[:10]:
            lines.append(f"      {key.file}: {key.function} — {key.property}")
        if len(new) > 10:
            lines.append(f"      ... and {len(new) - 10} more")
        lines.append("")
        lines.append("    Fix them, or accept them deliberately:")
        lines.append(f"      veripp accept {args.source} --baseline {args.baseline}")
    elif known:
        lines.append("    no new findings")
    if stale:
        # An entry matching nothing still grants permission, and will go on
        # granting it to whatever matches later.
        lines.append(f"    {len(stale)} baseline entr"
                     f"{'ies' if len(stale) != 1 else 'y'} no longer occur; "
                     "re-run `veripp accept` to drop them")
    return "\n".join(lines)


def _accept(args) -> int:
    """Record what is already there."""
    destination = args.baseline or Path(DEFAULT_BASELINE)

    reports = _collect_reports(args)
    if reports is None:
        return EXIT_USAGE

    keys = _findings_from(reports)
    baseline = Baseline()
    signatures = {
        key_for(r.source, f.name, f.detail): f.signature
        for r in reports for f in r.counterexamples
    }
    from .baseline import Entry
    from datetime import date

    today = date.today().isoformat()
    for key in keys:
        baseline.entries[key] = Entry(
            key=key, signature=signatures.get(key, ""),
            accepted=today, reason=args.reason,
        )
    baseline.save(destination)

    print(f"\nAccepted {len(keys)} finding{'s' if len(keys) != 1 else ''} "
          f"into {destination}")
    if keys:
        print("  Review it before committing: each entry is a risk someone")
        print("  decided to carry, and nothing will fail CI for it again.")
    print(f"\n  veripp scan {args.source} --baseline {destination}")
    print("      now fails only on findings that are not in there.")
    return EXIT_VERIFIED


def _collect_reports(args):
    """Scan one file or a whole tree, returning the reports."""
    if args.source.is_dir():
        sources = discover_sources(args.source)
        if not sources:
            print(f"error: no C or C++ source files under {args.source}",
                  file=sys.stderr)
            return None
    else:
        sources = [args.source]

    import copy

    reports = []
    for index, source in enumerate(sources, 1):
        per_file = copy.copy(args)
        per_file.source = source
        per_file._compdb_quiet = index > 1
        per_file._compdb_optional = True
        if not args.quiet and not args.json and len(sources) > 1:
            print(f"[{index}/{len(sources)}] {source}", file=sys.stderr)
        try:
            reports.append(scan(
                source, _config_for(per_file), _harness_options(per_file),
                jobs=args.jobs, escalations=args.escalations,
            ))
        except Exception as exc:
            print(f"  skipped ({type(exc).__name__}: {exc})", file=sys.stderr)
    return reports


def _scan_tree(args) -> int:
    """Scan every C/C++ file under a directory.

    Operating on a directory is what every neighbouring tool does -- ripgrep,
    fd, clang-tidy -- and requiring one file at a time is the difference
    between working on a file and working on a project.

    Each file is scanned independently and the findings are aggregated. Files
    are reported as they finish rather than at the end, because a tree scan
    can run for a long time and silence is indistinguishable from a hang.
    """
    sources = discover_sources(args.source)
    if not sources:
        print(f"error: no C or C++ source files under {args.source}",
              file=sys.stderr)
        print(f"  looked for: {', '.join(SOURCE_SUFFIXES)}", file=sys.stderr)
        print(f"  skipped: {', '.join(sorted(SKIP_DIRS)[:6])}, ... and dotted "
              "directories", file=sys.stderr)
        return EXIT_USAGE

    reports: list = []
    reused = 0

    # Everything derived from the source has to be derived per file. A
    # compilation database is keyed by translation unit, and the harness's
    # include path starts at the file's own directory -- neither means
    # anything for a directory. Resolving once against the tree root looked
    # the directory up in the database, which matches nothing: with an
    # explicit --compile-commands the scan died with a usage error before
    # verifying anything, and with an auto-discovered one every file silently
    # lost its include paths.
    import copy

    def settings_for(source: Path, first: bool):
        per_file = copy.copy(args)
        per_file.source = source
        # One note about a database that does not cover the tree is useful;
        # one per file is noise.
        per_file._compdb_quiet = not first
        # A tree legitimately contains files the database does not cover
        # (tests, fuzzers, generated code). Skip their flags, do not abort.
        per_file._compdb_optional = True
        return _config_for(per_file), _harness_options(per_file)

    if not args.quiet and not args.json:
        print(f"scanning {len(sources)} file"
              f"{'s' if len(sources) != 1 else ''} under {args.source}",
              file=sys.stderr)

    for index, source in enumerate(sources, 1):
        if not args.quiet and not args.json:
            print(f"\n[{index}/{len(sources)}] {source}", file=sys.stderr)

        def progress(done: int, total: int, result, _src=source) -> None:
            if args.quiet or args.json:
                return
            mark = {"verified": "PROVED", "counterexample": "COUNTEREX",
                    "refused": "skip"}.get(result.outcome, result.outcome)
            if result.artifact:
                mark = "artifact"
            painted = {
                "PROVED": ("green",), "COUNTEREX": ("red", "bold"),
            }.get(mark, ("dim",) if mark in ("skip", "artifact") else ("yellow",))
            print(f"  [{done:4d}/{total}] "
                  f"{term.style(f'{mark:>10}', *painted, stream=sys.stderr)}  "
                  f"{result.name}", file=sys.stderr)

        try:
            config, options = settings_for(source, first=index == 1)
            selected = _selected_names(args, source)
            if selected is not None and not selected:
                continue  # nothing here matches; not an error across a tree

            # --only asks for a subset, so its result is not this file's
            # verdict and must not be cached as one.
            cache = _cache_for(args) if selected is None else None
            key = _cache_key(args, source, config, options) if cache else ""
            cached = cache.get(key) if cache else None
            if cached is not None:
                reports.append(_report_from_cache(source, cached))
                reused += 1
                if not args.quiet and not args.json:
                    print("  (cached)", file=sys.stderr)
                continue

            report = scan(source, config, options, jobs=args.jobs,
                          progress=progress, escalations=args.escalations,
                          only=selected)
            if cache:
                cache.put(key, _cache_payload(report))
            reports.append(report)
        except Exception as exc:  # one unreadable file must not lose the rest
            print(f"  skipped ({type(exc).__name__}: {exc})", file=sys.stderr)

    payload = {
        "root": str(args.source),
        "files": len(reports),
        "candidates": sum(r.candidates for r in reports),
        "proved": sum(len(r.proved) for r in reports),
        "counterexamples": [
            {"file": str(r.source), "function": f.name, "property": f.detail}
            for r in reports for f in r.counterexamples
        ],
        "inconclusive": sum(len(r.inconclusive) for r in reports),
        "artifacts": sum(len(r.artifacts) for r in reports),
        "per_file": [
            {"file": str(r.source), "candidates": r.candidates,
             "proved": len(r.proved), "counterexamples": len(r.counterexamples),
             "inconclusive": len(r.inconclusive)}
            for r in reports
        ],
    }
    if reused and not args.quiet and not args.json:
        print(f"\n  {reused} of {len(sources)} file"
              f"{'s' if len(sources) != 1 else ''} were unchanged and reused "
              "from the cache", file=sys.stderr)
    _write_sarif(args, reports)
    verdict, extra = _apply_baseline(args, reports)
    payload.update(extra)
    _emit(args, payload,
          _tree_summary(args.source, reports) + _baseline_note(args, reports))
    return verdict


def _tree_summary(root: Path, reports: list) -> str:
    total_cx = sum(len(r.counterexamples) for r in reports)
    lines = [
        "",
        f"Scanned {len(reports)} file{'s' if len(reports) != 1 else ''} under {root}",
        f"  {sum(r.candidates for r in reports)} function definitions found",
        "",
        f"  PROVED           {sum(len(r.proved) for r in reports):4d}",
        f"  COUNTEREXAMPLE   {total_cx:4d}",
        f"  INCONCLUSIVE     {sum(len(r.inconclusive) for r in reports):4d}",
        f"  HARNESS ARTIFACT {sum(len(r.artifacts) for r in reports):4d}",
    ]
    if total_cx:
        lines += ["", "  files with findings:"]
        for report in reports:
            if report.counterexamples:
                names = ", ".join(f.name for f in report.counterexamples[:4])
                if len(report.counterexamples) > 4:
                    names += f", and {len(report.counterexamples) - 4} more"
                lines.append(f"    {report.source}: {names}")
        first = next(r for r in reports if r.counterexamples)
        lines += [
            "",
            f"  next:  veripp verify {first.source} "
            f"--function {first.counterexamples[0].name}",
            "         to see the failing input.",
        ]
    return "\n".join(lines)


def _scan(args) -> int:
    if args.source.is_dir():
        return _scan_tree(args)
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
        # Pad before colouring: escape codes have width on the terminal but
        # not on the screen, so padding a coloured string misaligns the column.
        painted = {
            "PROVED": ("green",), "COUNTEREX": ("red", "bold"),
        }.get(mark, ("dim",) if mark in ("skip", "artifact") else ("yellow",))
        print(f"[{done:4d}/{total}] "
              f"{term.style(f'{mark:>10}', *painted, stream=sys.stderr)}  {result.name}",
              file=sys.stderr)
        seen.append(result.name)

    selected = _selected_names(args, args.source)
    if selected is not None and not selected:
        print(f"error: --only {' '.join(args.only)} matched no function in "
              f"{args.source}", file=sys.stderr)
        print(f"  list them with:  veripp scan {args.source}", file=sys.stderr)
        return EXIT_USAGE

    cache = _cache_for(args) if selected is None else None
    key = _cache_key(args, args.source, config, options) if cache else ""
    cached = cache.get(key) if cache else None
    if cached is not None:
        report = _report_from_cache(args.source, cached)
        if not args.quiet and not args.json:
            print(f"  (cached: {args.source} unchanged since it was last "
                  "verified)", file=sys.stderr)
    else:
        report = scan(args.source, config, options, jobs=args.jobs,
                      progress=progress, escalations=args.escalations,
                      only=selected)
        if cache:
            cache.put(key, _cache_payload(report))

    scan_payload = {
            "source": str(report.source),
            "candidates": report.candidates,
            "proved": [r.name for r in report.proved],
            "counterexamples": [
                {"function": r.name, "signature": r.signature, "property": r.detail,
                 "assumptions": r.assumptions, "stubbed_calls": r.stubbed_calls,
                 "file": r.file, "line": r.line, "column": r.column, "cwes": r.cwes}
                for r in report.counterexamples
            ],
            "artifacts": [
                {"function": r.name, "property": r.detail, "why": r.artifact}
                for r in report.artifacts
            ],
            "inconclusive": [{"function": r.name, "outcome": r.outcome} for r in report.inconclusive],
            "not_harnessable": report.refusal_reasons(),
    }
    _write_sarif(args, [report])
    verdict, extra = _apply_baseline(args, [report])
    scan_payload.update(extra)
    _emit(args, scan_payload, report.summary() + _baseline_note(args, [report]))
    return verdict


def _emit(args, payload: dict, readable: str) -> None:
    """One run, both representations.

    --json replaces stdout, which is right for a shell pipeline but forces CI
    to choose between a log a human can read and a report a machine can parse
    -- or to verify twice to get both. --json-out writes the report to a file
    and leaves stdout alone, so one run serves both.
    """
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(readable)
    path = getattr(args, "json_out", None)
    if path:
        Path(path).write_text(json.dumps(payload, indent=2, default=str))


def _completion_script(shell: str, sub) -> str:
    """A completion script generated from the parser itself.

    Hand-written completions rot: a flag gets added, nobody updates the script,
    and the shell quietly suggests options that no longer exist. Walking the
    real parser means the completions are correct by construction.
    """
    commands = [name for name in sub.choices if name != "completion"]

    # argparse keeps each subcommand's one-line help on the _SubParsersAction,
    # not on the subparser, and `description` is usually empty -- indexing
    # splitlines()[0] on it raises.
    helps = {
        action.dest: (action.help or action.dest)
        for action in getattr(sub, "_choices_actions", [])
    }

    def describe(name: str) -> str:
        return (helps.get(name) or name).splitlines()[0][:60] or name
    flags: dict[str, list[str]] = {}
    for name, parser in sub.choices.items():
        flags[name] = sorted(
            option
            for action in parser._actions
            for option in action.option_strings
            if option.startswith("--")
        )

    if shell == "bash":
        cases = "\n".join(
            f'    {name}) opts="{" ".join(flags[name])}" ;;' for name in sub.choices
        )
        return f"""# veripp bash completion. eval "$(veripp completion bash)"
_veripp() {{
  local cur prev cmd opts
  cur="${{COMP_WORDS[COMP_CWORD]}}"
  prev="${{COMP_WORDS[COMP_CWORD-1]}}"
  cmd="${{COMP_WORDS[1]}}"

  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=($(compgen -W "{" ".join(commands)} completion --help --version" -- "$cur"))
    return
  fi
  if [ "$cmd" = "completion" ]; then
    COMPREPLY=($(compgen -W "bash zsh fish" -- "$cur"))
    return
  fi
  case "$prev" in
    --compile-commands|--link|--include-file|--json-out) COMPREPLY=($(compgen -f -- "$cur")); return ;;
    -I) COMPREPLY=($(compgen -d -- "$cur")); return ;;
  esac
  case "$cmd" in
{cases}
  esac
  if [[ "$cur" == -* ]]; then
    COMPREPLY=($(compgen -W "$opts" -- "$cur"))
  else
    COMPREPLY=($(compgen -f -X '!*.@(c|cc|cpp|cxx|h|hpp)' -- "$cur") $(compgen -d -- "$cur"))
  fi
}}
complete -F _veripp veripp"""

    if shell == "zsh":
        described = "\n".join(
            f"      '{name}:{describe(name)}'"
            for name in commands
        )
        per_command = "\n".join(
            f"    {name}) _arguments {' '.join(repr(f) for f in flags[name])} '*:file:_files' ;;"
            for name in sub.choices
        )
        return f"""#compdef veripp
# veripp zsh completion. eval "$(veripp completion zsh)"
_veripp() {{
  local -a commands
  commands=(
{described}
      'completion:print a shell completion script'
  )
  if (( CURRENT == 2 )); then
    _describe 'command' commands
    return
  fi
  case "${{words[2]}}" in
    completion) _values 'shell' bash zsh fish ;;
{per_command}
  esac
}}
compdef _veripp veripp"""

    all_flags = sorted({flag for group in flags.values() for flag in group})
    lines = [
        "# veripp fish completion. veripp completion fish | source",
        "complete -c veripp -f",
    ]
    for name in commands:
        help_text = describe(name)
        lines.append(
            f"complete -c veripp -n '__fish_use_subcommand' -a {name} -d {help_text!r}"
        )
    lines.append(
        "complete -c veripp -n '__fish_use_subcommand' -a completion "
        "-d 'print a shell completion script'"
    )
    for flag in all_flags:
        lines.append(f"complete -c veripp -n 'not __fish_use_subcommand' -l {flag[2:]}")
    return "\n".join(lines)


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

    readable = report.summary()
    if harness and not args.keep_harness:
        readable += f"\n(harness kept at {target})"
    _emit(args, _payload(report, harness), readable)

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
    quiet = quiet or getattr(args, "_compdb_quiet", False)
    try:
        entry = entry_for(database, args.source)
    except CompDBError as exc:
        # Auto-discovery must never break a run that would otherwise work.
        if args.compile_commands is not None and not getattr(
            args, "_compdb_optional", False
        ):
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


DOCKER_HINT = (
    'docker run --rm -v "$PWD:/src" ghcr.io/gfabbretti8/veripp scan FILE.c'
)


def _missing_source_hint() -> str | None:
    """The single most likely reason a path is missing inside the image.

    Running the container without -v leaves the working directory empty, and
    "error: foo.c not found" is a true but useless thing to tell someone whose
    file is sitting right there on their host. Only fires when we are actually
    in the image and the working directory really is empty, so it cannot
    misdirect someone who simply mistyped a filename.
    """
    import os

    if os.environ.get("VERIPP_IN_CONTAINER") != "1":
        return None
    workdir = Path("/src")
    try:
        empty = not any(workdir.iterdir())
    except PermissionError:
        # Mounted, but the image's non-root user cannot read it -- the usual
        # cause is a project under a 0700 home directory. Saying "not found"
        # here sends people looking for a typo that is not there.
        return (
            "hint: /src is mounted but this container cannot read it.\n"
            "      The image runs as a non-root user, and the directory you\n"
            "      mounted is not readable by it. Either loosen its mode, or\n"
            '      re-run with:  --user "$(id -u):$(id -g)"'
        )
    except OSError:
        return None
    if not empty:
        return None
    return (
        "hint: /src is empty, so nothing was mounted into the container.\n"
        '      Mount your project there:  docker run --rm -v "$PWD:/src" '
        "IMAGE " + " ".join(sys.argv[1:2] or ["scan"]) + " FILE"
    )


def _esbmc_install_hint() -> str:
    """The exact command for *this* machine, not a link to go read.

    Architecture matters more than it looks. ESBMC publishes one Linux binary
    and it is x86_64; handing an aarch64 user that URL gets them a download
    that will not execute. The only prebuilt arm64 Linux ESBMC anywhere is the
    Homebrew bottle, pinned to 8.4, which is the release that silently misses
    out-of-bounds writes (esbmc#6508) -- so recommending it would trade a
    clear failure for a quiet one. On that platform the image is the answer.
    """
    import platform

    system = platform.system()
    machine = platform.machine().lower()

    if system == "Darwin":
        # The macOS release zip links against Homebrew's z3/gmp/mpfr by
        # absolute path, so it is not relocatable; brew is the only sane route.
        return "brew install --HEAD esbmc"

    if system == "Linux" and machine in ("x86_64", "amd64"):
        return (
            "curl -fsSL -o /tmp/esbmc.zip "
            "https://github.com/esbmc/esbmc/releases/download/weekly/esbmc-linux.zip "
            "&& unzip -q /tmp/esbmc.zip -d ~/.local/esbmc "
            "&& chmod +x ~/.local/esbmc/*/bin/esbmc"
        )

    if system == "Linux":
        return (
            f"{DOCKER_HINT}\n"
            f"      (no prebuilt ESBMC is published for Linux/{machine}; the image "
            "carries one built from source)"
        )

    return DOCKER_HINT


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
                mark = (term.style("ok  ", "green") if ok
                        else term.style("FAIL", "red", "bold"))
                print(f"  {mark}  {name}")
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
