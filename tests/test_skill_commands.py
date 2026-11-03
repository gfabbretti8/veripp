"""Run what the skill tells an agent to run.

A skill is a set of instructions an agent follows literally. If a documented
invocation is rejected as a usage error, the agent burns a turn, retries
something it invented, and learns to distrust the tool -- which is worse than
having no skill at all. So every flag combination the skill recommends is
executed here against real fixtures.

Verdicts are not asserted: 0 (verified), 1 (counterexample) and 3
(inconclusive) are all legitimate answers about a given file. What must never
happen is exit 2 -- veripp did not understand the command -- or a traceback.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills/veripp/SKILL.md"
EXIT_USAGE = 2

pytestmark = pytest.mark.esbmc


def veripp(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "veripp.cli", *args],
        capture_output=True, text=True, cwd=cwd or ROOT, timeout=900,
    )


def check(result: subprocess.CompletedProcess, what: str) -> None:
    combined = result.stdout + result.stderr
    assert "Traceback" in combined is False or "Traceback" not in combined, (
        f"{what} crashed:\n{combined[-1500:]}"
    )
    assert result.returncode != EXIT_USAGE, (
        f"the skill recommends `{what}`, and veripp rejected it as a usage "
        f"error:\n{combined[-1500:]}"
    )


#: A C API that hands out a handle -- what --sequence and --constructors are for.
LIST_C = """\
#include <stdlib.h>

typedef struct list { int items[4]; unsigned len; } list_t;

list_t *list_new(void) {
    list_t *l = malloc(sizeof *l);
    if (l) l->len = 0;
    return l;
}
void list_free(list_t *l) { free(l); }
int list_push(list_t *l, int v) {
    if (l->len >= 4) return -1;
    l->items[l->len++] = v;
    return 0;
}
int list_pop(list_t *l) { return l->len > 0 ? l->items[--l->len] : -1; }
"""

#: A module whose state only its own init() sets up, and a caller of it.
MEM_C = """\
static char *heap;
static char arena[64];
void mem_init(void) { heap = arena; }
char *mem_malloc(unsigned n) { return n <= sizeof arena ? heap : 0; }
"""
HTTPD_C = """\
char *mem_malloc(unsigned n);
int parse_request(unsigned n) {
    if (n == 0 || n > 64) return -1;
    char *buf = mem_malloc(n);
    buf[0] = 'G';
    return buf[n - 1];
}
"""

#: A walk to NUL, which is only safe if the caller hands over a C string.
NAME_C = """\
int name_decode(const char *name) {
    int n = 0;
    while (name[n]) n++;
    return n;
}
"""

#: A struct with a member inside #if, which veripp refuses to fill as text.
LCP_C = """\
#include <stdlib.h>
#define LCP_OPTS 1
typedef struct lcp {
    int state;
#if LCP_OPTS
    int opts[4];
#endif
} lcp_t;
lcp_t *lcp_new(void) { return calloc(1, sizeof(lcp_t)); }
void lcp_free(lcp_t *l) { free(l); }
int lcp_input(lcp_t *l, unsigned i) {
    if (i >= 4) return -1;
    return l->opts[i] + l->state;
}
"""


class TestDocumentedInvocations:
    """Each of these mirrors a command block in SKILL.md, with the
    illustrative paths swapped for fixtures that exist."""

    def test_doctor(self) -> None:
        check(veripp("doctor"), "veripp doctor")

    def test_scan_a_file(self) -> None:
        check(veripp("scan", "examples/ring_buffer.cpp"), "veripp scan FILE")

    def test_verify_a_function(self) -> None:
        check(
            veripp("verify", "examples/off_by_one.cpp", "--function", "sum_array"),
            "veripp verify FILE --function F",
        )

    def test_scan_with_jobs_and_json(self) -> None:
        result = veripp("scan", "examples/ring_buffer.cpp", "--jobs", "4", "--json")
        check(result, "veripp scan FILE --jobs 4 --json")
        json.loads(result.stdout)  # must be parseable, since the skill says so

    def test_verify_with_an_assumption(self) -> None:
        check(
            veripp("verify", "examples/off_by_one.cpp", "--function", "sum_array",
                   "--assume", "n > 0"),
            "veripp verify FILE --function F --assume EXPR",
        )

    def test_class_with_max_calls_and_assert(self) -> None:
        check(
            veripp("verify", "examples/ring_buffer.cpp", "--class", "RingBuffer",
                   "--max-calls", "4", "--assert", "true"),
            "veripp verify FILE --class C --max-calls N --assert EXPR",
        )

    def test_harness_prints_without_verifying(self) -> None:
        result = veripp("harness", "examples/off_by_one.cpp", "--function", "sum_array")
        check(result, "veripp harness FILE --function F")
        assert "VERIPP_NONDET" in result.stdout

    def test_scan_a_directory(self, tmp_path) -> None:
        """SKILL.md now tells the agent to prefer `veripp scan src/` over
        scanning files one at a time, so that form has to work."""
        src = tmp_path / "src"
        (src / "sub").mkdir(parents=True)
        (src / "a.c").write_text("int f(int x){ return x; }\n", encoding="utf-8")
        (src / "sub" / "b.c").write_text("int g(int x){ return x; }\n", encoding="utf-8")
        result = veripp("scan", str(src), "--jobs", "8")
        check(result, "veripp scan DIR --jobs N")
        assert "2 files" in result.stdout, result.stdout[-500:]

    def test_json_out_alongside_readable_output(self, tmp_path) -> None:
        report = tmp_path / "report.json"
        result = veripp("scan", "examples/ring_buffer.cpp", "--json-out", str(report))
        check(result, "veripp scan FILE --json-out PATH")
        assert "Scanned" in result.stdout, "readable output was replaced, not kept"
        assert json.loads(report.read_text(encoding="utf-8"))["candidates"]

    def test_compile_commands(self, tmp_path) -> None:
        """The skill shows --compile-commands; make sure the flag works with a
        database, including one written for a different absolute root."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.c").write_text("int scale(int x){ return x * 2; }\n", encoding="utf-8")
        db = tmp_path / "build"
        db.mkdir()
        (db / "compile_commands.json").write_text(json.dumps([{
            "directory": str(db), "file": str(src / "a.c"),
            "command": f"cc -c {src / 'a.c'}",
        }]))
        check(
            veripp("verify", "src/a.c", "--function", "scale",
                   "--compile-commands", "build/compile_commands.json", cwd=tmp_path),
            "veripp verify FILE --compile-commands DB",
        )


    def test_scan_only_what_changed(self, tmp_path) -> None:
        def git(*args: str) -> None:
            subprocess.run(
                ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                cwd=tmp_path, check=True, capture_output=True,
            )

        git("init", "-q", "-b", "main")
        (tmp_path / "old.c").write_text("int f(int x){ return x; }\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-qm", "base")
        git("checkout", "-qb", "feature")
        (tmp_path / "new.c").write_text("int g(int x){ return x; }\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-qm", "change")
        result = veripp("scan", ".", "--changed", "main", cwd=tmp_path)
        check(result, "veripp scan . --changed REF")
        assert "Scanned 1 file" in result.stdout, result.stdout[-500:]

        (tmp_path / "wip.c").write_text("int h(int x){ return x; }\n", encoding="utf-8")
        result = veripp("scan", ".", "--changed", cwd=tmp_path)
        check(result, "veripp scan . --changed")
        assert "Scanned 1 file" in result.stdout, result.stdout[-500:]

    def test_repro_of_a_counterexample(self, tmp_path) -> None:
        out = tmp_path / "repro.cpp"
        check(
            veripp("verify", "examples/off_by_one.cpp", "--function", "sum_array",
                   "--repro", str(out)),
            "veripp verify FILE --function F --repro PATH",
        )
        assert out.is_file(), "a counterexample, and no reproducer written"

    def test_scan_with_a_retry_budget(self, tmp_path) -> None:
        (tmp_path / "m.c").write_text("int f(int x){ return x; }\n", encoding="utf-8")
        check(veripp("scan", "m.c", "--retry-budget", "900", cwd=tmp_path),
              "veripp scan FILE --retry-budget SECONDS")

    def test_sequence_over_a_c_api(self, tmp_path) -> None:
        (tmp_path / "list.c").write_text(LIST_C, encoding="utf-8")
        result = veripp("verify", "list.c", "--sequence", "list_t",
                        "--sequence-call", "list_push*", cwd=tmp_path)
        check(result, "veripp verify FILE --sequence TYPE --sequence-call GLOB")
        assert "list_new returns" in result.stdout, "not built by the library"

    def test_setup_before_a_linked_module(self, tmp_path) -> None:
        (tmp_path / "mem.c").write_text(MEM_C, encoding="utf-8")
        (tmp_path / "httpd.c").write_text(HTTPD_C, encoding="utf-8")
        result = veripp("verify", "httpd.c", "--function", "parse_request",
                        "--link", "mem.c", "--setup", "mem_init()", cwd=tmp_path)
        check(result, "veripp verify FILE --function F --link SRC --setup CALL")
        assert "runs mem_init() first" in result.stdout, "the setup went unsaid"

    def test_unterminated_bytes(self, tmp_path) -> None:
        (tmp_path / "name.c").write_text(NAME_C, encoding="utf-8")
        result = veripp("verify", "name.c", "--function", "name_decode",
                        "--unterminated", cwd=tmp_path)
        check(result, "veripp verify FILE --function F --unterminated")
        assert "NO terminator" in result.stdout, "the model went unsaid"

    def test_preprocess_and_constructors(self, tmp_path) -> None:
        """Without either flag this struct is refused, which is exit 2."""
        (tmp_path / "lcp.c").write_text(LCP_C, encoding="utf-8")
        check(
            veripp("verify", "lcp.c", "--function", "lcp_input",
                   "--preprocess", "--constructors", cwd=tmp_path),
            "veripp verify FILE --function F --preprocess --constructors",
        )


class TestSkillStaysTrue:
    def test_every_documented_flag_is_exercised_above(self) -> None:
        """If SKILL.md gains an invocation, this file should gain a test.

        Guards the drift where a skill accumulates examples nobody ever runs,
        which is how documented commands quietly stop working.
        """
        import inspect

        blocks = re.findall(r"```bash\n(.*?)```", SKILL.read_text(encoding="utf-8"), re.S)
        documented = {
            flag
            for block in blocks
            for line in block.splitlines()
            if line.strip().startswith("veripp ")
            for flag in re.findall(r"(?<![\w-])--[a-z][a-z0-9-]+", line)
        }
        # The whole module, not one class: a test that exercises a flag
        # counts wherever it lives, and tying this to a single class means
        # adding a new class silently stops satisfying the guard.
        exercised = set(
            re.findall(r'"(--[a-z][a-z0-9-]+)"',
                       pathlib.Path(__file__).read_text(encoding="utf-8"))
        )
        missing = documented - exercised
        assert not missing, (
            f"SKILL.md documents {sorted(missing)} but nothing here runs them"
        )

    def test_every_documented_subcommand_is_exercised(self) -> None:
        """Flags are not the only thing that drifts. `veripp scan DIR` added
        no new flag, so the flag guard above would not have noticed it."""
        import inspect

        blocks = re.findall(r"```bash\n(.*?)```", SKILL.read_text(encoding="utf-8"), re.S)
        documented = {
            line.strip().split()[1]
            for block in blocks
            for line in block.splitlines()
            if line.strip().startswith("veripp ") and len(line.strip().split()) > 1
        }
        source = inspect.getsource(TestDocumentedInvocations)
        exercised = set(re.findall(r'veripp\(\s*"([a-z]+)"', source))
        missing = {c for c in documented if c.startswith(("verify", "scan", "harness",
                                                          "doctor", "completion"))}
        missing = {c.split()[0] for c in missing} - exercised
        assert not missing, (
            f"SKILL.md documents subcommands nothing here runs: {sorted(missing)}"
        )

    def test_the_skill_covers_the_commands_the_cli_offers(self) -> None:
        """The other direction, which nothing checked.

        The existing guard fails when SKILL.md documents a flag no test runs.
        Nothing caught the reverse: the CLI growing past the skill. `accept`,
        `--baseline`, `--sarif`, `--cache` and `--only` all shipped while the
        skill said nothing about them, so an agent following it would never
        learn the baseline exists -- and the baseline is what makes veripp
        usable on a codebase that already has findings.
        """
        import subprocess
        import sys as _sys

        text = SKILL.read_text(encoding="utf-8")

        result = subprocess.run(
            [_sys.executable, "-m", "veripp.cli", "--help"],
            capture_output=True, text=True, cwd=ROOT, timeout=300,
        )
        commands = set(re.findall(r"^\s{4}(\w+)\s{2,}", result.stdout, re.M))
        # `completion` is shell setup, not something an agent drives.
        commands -= {"completion"}
        missing = sorted(c for c in commands if c not in text)
        assert not missing, (
            f"the CLI offers {missing} and SKILL.md never mentions them"
        )

    #: Flags an agent has no reason to drive: output plumbing, escape
    #: hatches, and build settings compile_commands.json already carries --
    #: documented in --help where someone looking for them will be.
    NOT_FOR_AGENTS = {
        "--help", "--version", "--quiet", "--no-llm", "--llm-base-url",
        "--no-cache", "--no-compile-commands", "--no-initializers",
        "--no-overflow-check", "--allow-unsound", "--keep-harness", "--dir",
        "--reason", "--escalations", "--max-struct-depth", "--include-file",
        "--std", "--define", "--include", "--force", "--dry-run", "--global",
        "--yes",
    }

    def test_the_skill_covers_the_flags_worth_driving(self) -> None:
        """Every flag the CLI has is in SKILL.md, or listed above as not for
        agents. A hand-picked list of "important" flags was the check
        before, and six flags that change what veripp verifies -- --sequence,
        --sequence-call, --setup, --unterminated, --preprocess and
        --constructors -- shipped past it without a word in the skill. A new
        flag now has to be placed on one side or the other."""
        import subprocess
        import sys as _sys

        text = SKILL.read_text(encoding="utf-8")
        # The completion script is generated from the parser itself, hidden
        # flags included, so it is the full list rather than --help's.
        result = subprocess.run(
            [_sys.executable, "-m", "veripp.cli", "completion", "bash"],
            capture_output=True, text=True, cwd=ROOT, timeout=300,
        )
        assert result.returncode == 0, result.stderr
        flags = set(re.findall(r"--[a-z][\w-]*", " ".join(
            re.findall(r'opts="([^"]*)"', result.stdout))))
        assert "--sequence" in flags, "the flag list came back incomplete"
        missing = sorted(
            flag for flag in flags - self.NOT_FOR_AGENTS
            if not re.search(re.escape(flag) + r"(?![\w-])", text)
        )
        assert not missing, (
            f"SKILL.md never mentions {missing}. Document each where an agent "
            "would reach for it, or add it to NOT_FOR_AGENTS if it has no "
            "reason to."
        )


@pytest.mark.esbmc
class TestBaselineWorkflowFromTheSkill:
    """The skill now tells an agent to set up a baseline on existing code.
    That whole sequence has to work, not just its individual flags."""

    def test_the_documented_baseline_sequence(self, tmp_path) -> None:
        (tmp_path / "m.c").write_text(
            "int mean(int a,int b){ return (a+b)/2; }\n", encoding="utf-8"
        )
        accepted = veripp("accept", "m.c", "--baseline", ".veripp-baseline", cwd=tmp_path)
        check(accepted, "veripp accept FILE --baseline PATH")
        after = veripp("scan", "m.c", "--baseline", ".veripp-baseline", cwd=tmp_path)
        assert after.returncode == 0, "an accepted finding still failed the run"

    def test_sarif_and_baseline_together(self, tmp_path) -> None:
        """The combination the skill recommends for CI."""
        import json

        (tmp_path / "m.c").write_text(
            "int mean(int a,int b){ return (a+b)/2; }\n", encoding="utf-8"
        )
        veripp("accept", "m.c", "--baseline", ".veripp-baseline", cwd=tmp_path)
        result = veripp("scan", "m.c", "--sarif", "out.sarif",
                        "--baseline", ".veripp-baseline", cwd=tmp_path)
        check(result, "veripp scan FILE --sarif PATH --baseline PATH")
        log = json.loads((tmp_path / "out.sarif").read_text(encoding="utf-8"))
        assert log["runs"][0]["results"][0]["suppressions"], (
            "a baselined finding should be uploaded as suppressed, not dropped"
        )

    def test_only_and_cache(self, tmp_path) -> None:
        (tmp_path / "m.c").write_text(
            "int parse_one(int x){ return x; }\nint other(int x){ return x; }\n",
            encoding="utf-8",
        )
        check(veripp("scan", "m.c", "--only", "parse_*", cwd=tmp_path),
              "veripp scan FILE --only GLOB")
        check(veripp("scan", "m.c", "--cache", str(tmp_path / "c"), cwd=tmp_path),
              "veripp scan FILE --cache DIR")
        check(veripp("scan", "m.c", "--no-cache", cwd=tmp_path),
              "veripp scan FILE --no-cache")

    def test_raising_the_budget(self, tmp_path) -> None:
        (tmp_path / "m.c").write_text("int f(int x){ return x; }\n", encoding="utf-8")
        check(veripp("verify", "m.c", "--function", "f", "--unwind", "64",
                     "--timeout", "300", cwd=tmp_path),
              "veripp verify FILE --unwind N --timeout N")


class TestTheEscapeHatchTheSkillDocuments:
    """SKILL.md tells agents that one flag reaches every ESBMC feature.

    If that stops being true the skill is teaching a lie, so the documented
    invocation is run here rather than trusted.
    """

    def test_esbmc_arg_reaches_the_checker(self, tmp_path):
        src = tmp_path / "ok.c"
        src.write_text("unsigned twice(unsigned x){ return x / 2u; }\n")
        result = veripp(
            "verify", str(src), "--function", "twice",
            "--esbmc-arg", "--struct-fields-check", "--no-llm",
        )
        check(result, "verify with --esbmc-arg")
        combined = result.stdout + result.stderr
        assert "--struct-fields-check" in combined, (
            "a raw ESBMC flag must be named in the result, or a weakened "
            "proof reads the same as a real one"
        )

    def test_help_all_reveals_what_help_hides(self):
        shown = veripp("verify", "--help").stdout
        every = veripp("verify", "--help-all").stdout
        assert "--unwind" not in shown and "--unwind" in every
