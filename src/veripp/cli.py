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
        # Pad before colouring: escape codes have width on the terminal but
        # not on the screen, so padding a coloured string misaligns the column.
        painted = {
            "PROVED": ("green",), "COUNTEREX": ("red", "bold"),
        }.get(mark, ("dim",) if mark in ("skip", "artifact") else ("yellow",))
        print(f"[{done:4d}/{total}] "
              f"{term.style(f'{mark:>10}', *painted, stream=sys.stderr)}  {result.name}",
              file=sys.stderr)
        seen.append(result.name)

    report = scan(args.source, config, options, jobs=args.jobs, progress=progress,
                  escalations=args.escalations)

    scan_payload = {
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
    }
    _emit(args, scan_payload, report.summary())
    return EXIT_VERIFIED if not report.counterexamples else EXIT_COUNTEREXAMPLE


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
