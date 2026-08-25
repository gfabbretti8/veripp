"""The delivery surface: image, action, skill.

These files are not exercised by the rest of the suite -- they only run on a
CI runner or a user's machine -- so mistakes in them are invisible until
someone tries to use the tool. That is exactly how `action.yml` shipped with
`uv run --directory`, which changes the working directory and therefore
resolved every user-supplied relative path against the action's own checkout
instead of the caller's workspace.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def read(name: str) -> str:
    return (ROOT / name).read_text()


class TestAction:
    def test_never_uses_uv_directory(self) -> None:
        """--directory changes cwd; --project does not.

        With --directory, `veripp scan src/parser.c` looks for
        <action_path>/src/parser.c. Relative paths are the normal case, so
        this breaks essentially every invocation.
        """
        script = "\n".join(
            line for line in read("action.yml").splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "--directory" not in script, (
            "action.yml must use `uv run --project`, not `--directory`: "
            "--directory changes the working directory and breaks every "
            "relative source path the caller passes."
        )
        assert "--project" in read("action.yml")

    def test_inputs_are_not_interpolated_into_the_shell(self) -> None:
        """`${{ inputs.x }}` inside `run:` splices attacker-controlled text.

        On pull_request, inputs derive from the head repo. Passing them through
        `env:` keeps them as values instead of script source.
        """
        text = read("action.yml")
        run_blocks = re.findall(r"run: \|(.*?)(?=\n    - |\Z)", text, re.S)
        for block in run_blocks:
            leaked = re.findall(r"\$\{\{\s*inputs\.[\w-]+\s*\}\}", block)
            assert not leaked, (
                f"action.yml interpolates {leaked} directly into a run: block; "
                "pass it through env: instead"
            )

    def test_the_verify_step_disables_errexit_around_the_run(self) -> None:
        """GitHub runs composite `shell: bash` as `bash -e -o pipefail`.

        veripp exits 1 on a counterexample and 3 when inconclusive -- both
        ordinary outcomes the action is supposed to interpret. Under -e the
        script dies at the veripp call instead, so no annotation is printed,
        `fail-on: never` never runs, and an inconclusive result fails the job
        rather than warning. `set -uo pipefail` does NOT clear -e; only
        `set +e` does. This shipped, and only a real runner caught it.
        """
        text = read("action.yml")
        block = text[text.index("Run veripp") :]
        run_body = block[block.index("run: |") :]
        assert "set +e" in run_body, (
            "the step must `set +e` before invoking veripp: GitHub's composite "
            "bash runs with -e, which aborts on veripp's meaningful non-zero "
            "exit codes before fail-on can be honoured"
        )
        assert run_body.index("set +e") < run_body.index("status=$?"), (
            "`set +e` must come before the veripp invocation, not after"
        )

    def test_defaults_to_the_sound_esbmc(self) -> None:
        """8.4 silently misses out-of-bounds writes (esbmc#6508)."""
        text = read("action.yml")
        assert "default: 'weekly'" in text or 'default: "weekly"' in text


class TestDockerfile:
    def test_builds_both_architectures(self) -> None:
        text = read("Dockerfile")
        assert "AS esbmc-amd64" in text
        assert "AS esbmc-arm64" in text
        # The selector is what makes one Dockerfile serve both.
        assert "FROM esbmc-${TARGETARCH}" in text

    def test_arm64_builds_from_source(self) -> None:
        """No sound prebuilt arm64 Linux ESBMC exists.

        The only prebuilt one is the Homebrew bottle at 8.4, which carries
        esbmc#6508. If this stage ever becomes a download, that is a silent
        soundness regression for every arm64 user, so pin the intent here.
        """
        text = read("Dockerfile")
        arm_stage = text.split("AS esbmc-arm64", 1)[1].split("\nFROM ", 1)[0]
        # Judge the instructions, not the comments explaining them.
        instructions = "\n".join(
            line for line in arm_stage.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "cmake --build" in instructions or "build.sh" in instructions, (
            "the arm64 stage must compile ESBMC from source"
        )
        assert "esbmc-linux.zip" not in instructions, (
            "esbmc-linux.zip is x86_64; it cannot be used for arm64"
        )

    def test_arm64_disables_the_32bit_libc_model(self) -> None:
        """arm64 has no 32-bit multilib, so that model cannot be built.

        esbmc's own scripts/build.sh skips g++-multilib on aarch64 and then
        asks for the 32-bit libc model anyway, which fails on a missing
        bits/libc-header-start.h. Homebrew's formula turns it off on Linux for
        the same reason.
        """
        text = read("Dockerfile")
        arm_stage = text.split("AS esbmc-arm64", 1)[1].split("\nFROM ", 1)[0]
        assert "-DENABLE_BUNDLE_LIBC_32BIT=OFF" in arm_stage

    def test_the_build_refuses_to_ship_an_unsound_checker(self) -> None:
        assert re.search(r"^RUN veripp doctor", read("Dockerfile"), re.M), (
            "the image build must run `veripp doctor`, so an ESBMC that "
            "cannot find a planted bug fails the build instead of shipping"
        )

    def test_does_not_run_as_root(self) -> None:
        assert re.search(r"^USER ", read("Dockerfile"), re.M)

    def test_dockerignore_excludes_the_local_venv(self) -> None:
        """A host .venv baked into the image carries host-arch binaries."""
        ignored = read(".dockerignore").split()
        for entry in (".venv", ".git"):
            assert entry in ignored, f"{entry} must be in .dockerignore"


class TestSkill:
    PATH = "skills/veripp/SKILL.md"

    def test_has_well_formed_frontmatter(self) -> None:
        text = read(self.PATH)
        assert text.startswith("---\n")
        front = text.split("---", 2)[1]
        assert re.search(r"^name: veripp$", front, re.M)
        description = re.search(r"^description: (.+)$", front, re.M)
        assert description, "a skill without a description is never selected"
        # The description is the only thing an agent sees when deciding whether
        # to load the skill, so it has to say when to use it, not just what it is.
        assert len(description.group(1)) > 80

    def test_tells_the_agent_not_to_hand_write_harnesses(self) -> None:
        """The one thing that separates this from driving ESBMC directly."""
        text = read(self.PATH)
        assert "You do not write the verification harness" in text

    def test_warns_about_the_unsound_release(self) -> None:
        assert "6508" in read(self.PATH)

    def test_every_flag_it_recommends_exists(self) -> None:
        """A skill that suggests a flag the CLI does not have wastes a whole
        agent turn on an unparseable command and teaches it to distrust the
        tool. Cheap to check, so check it."""
        import subprocess
        import sys

        text = read(self.PATH)
        # Flags belonging to other commands the skill mentions -- docker, and
        # the skill's own install.sh -- are not veripp flags.
        foreign = {"--rm", "--platform", "--entrypoint", "--user", "--yes"}
        referenced = {
            flag
            for flag in re.findall(r"(?<![\w-])--[a-z][a-z0-9-]+", text)
            if flag not in foreign
        }

        advertised: set[str] = set()
        for sub in ([], ["verify"], ["scan"], ["harness"], ["doctor"]):
            result = subprocess.run(
                [sys.executable, "-m", "veripp.cli", *sub, "--help"],
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            advertised |= set(
                re.findall(r"(?<![\w-])--[a-z][a-z0-9-]+", result.stdout)
            )

        missing = sorted(referenced - advertised)
        assert not missing, f"SKILL.md recommends flags the CLI lacks: {missing}"

    def test_exit_codes_match_the_cli(self) -> None:
        from veripp.cli import (
            EXIT_COUNTEREXAMPLE,
            EXIT_INCONCLUSIVE,
            EXIT_USAGE,
        )

        text = read(self.PATH)
        for code, meaning in (
            (EXIT_COUNTEREXAMPLE, "COUNTEREXAMPLE"),
            (EXIT_USAGE, "Usage error"),
            (EXIT_INCONCLUSIVE, "Inconclusive"),
        ):
            assert re.search(rf"^\| {code} \| {meaning}", text, re.M), (
                f"SKILL.md documents exit code {code} incorrectly"
            )


class TestPluginPackaging:
    """The layout Claude Code's loader requires, which is easy to get subtly
    wrong: `.claude-plugin/` holds only the manifests, and everything else --
    skills/, commands/, agents/ -- lives at the plugin root, not inside it."""

    def test_manifests_are_valid_json(self) -> None:
        import json

        for name in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"):
            json.loads(read(name))

    def test_plugin_name_is_kebab_case(self) -> None:
        import json

        name = json.loads(read(".claude-plugin/plugin.json"))["name"]
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name), name

    def test_skills_live_at_the_plugin_root(self) -> None:
        assert (ROOT / "skills/veripp/SKILL.md").is_file(), (
            "skills must be at <root>/skills/, not inside .claude-plugin/, "
            "or the plugin loader will not find them"
        )
        assert not (ROOT / ".claude-plugin/skills").exists()

    def test_marketplace_points_at_a_real_plugin(self) -> None:
        import json

        market = json.loads(read(".claude-plugin/marketplace.json"))
        assert market["owner"]["name"]
        for entry in market["plugins"]:
            source = entry["source"]
            assert source.startswith("./"), source
            manifest = ROOT / source / ".claude-plugin/plugin.json"
            assert manifest.is_file(), f"{source} has no plugin manifest"
            assert json.loads(manifest.read_text())["name"] == entry["name"]

    def test_plugin_version_matches_the_package(self) -> None:
        import json

        manifest = json.loads(read(".claude-plugin/plugin.json"))
        pyproject = read("pyproject.toml")
        version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
        assert manifest["version"] == version, (
            f"plugin.json says {manifest['version']}, pyproject says {version}"
        )


class TestManifestRehearsal:
    """The release's manifest step cannot be tested by building an image, so
    there is a script that rehearses it against a local registry. Keep it
    runnable and keep it checking the thing that matters."""

    PATH = "tests/manifest_rehearsal.sh"

    def test_is_executable_and_valid_bash(self) -> None:
        import subprocess

        assert (ROOT / self.PATH).stat().st_mode & stat.S_IXUSR
        result = subprocess.run(
            ["bash", "-n", str(ROOT / self.PATH)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_asserts_both_architectures(self) -> None:
        text = read(self.PATH)
        assert "linux/amd64" in text and "linux/arm64" in text
        assert "push-by-digest" in text, (
            "the rehearsal must use push-by-digest, which is what the release "
            "does and what the plain docker driver cannot do"
        )
        assert "imagetools create" in text


class TestSmokeTest:
    PATH = "tests/image_smoketest.sh"

    def test_is_executable(self) -> None:
        mode = (ROOT / self.PATH).stat().st_mode
        assert mode & stat.S_IXUSR, f"{self.PATH} must be executable; CI runs it directly"

    def test_checks_a_proof_and_a_counterexample(self) -> None:
        text = read(self.PATH)
        assert "doctor" in text
        assert "VERIFIED" in text and "COUNTEREXAMPLE" in text

    @pytest.mark.skipif(os.name == "nt", reason="bash")
    def test_is_valid_bash(self) -> None:
        import subprocess

        result = subprocess.run(
            ["bash", "-n", str(ROOT / self.PATH)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr


class TestWorkflows:
    def test_all_workflows_parse(self) -> None:
        yaml = pytest.importorskip("yaml")
        files = list((ROOT / ".github/workflows").glob("*.yml")) + [ROOT / "action.yml"]
        assert files
        for path in files:
            yaml.safe_load(path.read_text())

    def test_image_workflow_verifies_the_manifest_covers_both_arches(self) -> None:
        text = read(".github/workflows/image.yml")
        assert "linux/amd64" in text and "linux/arm64" in text
        assert "imagetools inspect" in text, (
            "the release must assert both architectures made it into the "
            "manifest; a silently single-arch 'multi-arch' image is worse "
            "than none"
        )

    def test_arm64_uses_a_native_runner(self) -> None:
        """qemu turns the arm64 source build from long into unusable."""
        text = read(".github/workflows/image.yml")
        assert "ubuntu-24.04-arm" in text


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    """A checkout-like tree that is deliberately not the repository."""
    import shutil

    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")
    ws = tmp_path_factory.mktemp("workspace")
    (ws / "nested").mkdir()
    shutil.copy(ROOT / "examples/ring_buffer.cpp", ws / "nested")
    shutil.copy(ROOT / "examples/off_by_one.cpp", ws)
    return ws


@pytest.mark.esbmc
class TestActionCommandsFromAForeignWorkspace:
    """Run what the action runs, from somewhere that is not the action.

    The `--directory` bug was invisible to every other test because they all
    run with the repository as the working directory, where a path resolved
    against the action's checkout and a path resolved against the caller's
    workspace are the same path. The bug only appears when those two differ,
    which on a runner they always do.
    """

    @staticmethod
    def _run(workspace, *args):
        import subprocess

        return subprocess.run(
            ["uv", "run", "--project", str(ROOT), "veripp", *args],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=900,
        )

    def test_doctor(self, workspace) -> None:
        assert self._run(workspace, "doctor").returncode == 0

    def test_relative_path_in_a_subdirectory_resolves(self, workspace) -> None:
        """The precise shape --directory broke."""
        result = self._run(
            workspace, "verify", "nested/ring_buffer.cpp", "--function", "push"
        )
        assert "not found" not in result.stdout + result.stderr, (
            "the source was resolved against the wrong directory"
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_known_bug_still_exits_1(self, workspace) -> None:
        result = self._run(
            workspace, "verify", "off_by_one.cpp", "--function", "sum_array"
        )
        assert result.returncode == 1, result.stdout + result.stderr


class TestContainerHint:
    """`docker run IMAGE scan foo.c` without -v is the most likely first
    mistake anyone makes with the image, and "not found" does not explain it."""

    def test_silent_outside_the_container(self, monkeypatch) -> None:
        from veripp.cli import _missing_source_hint

        monkeypatch.delenv("VERIPP_IN_CONTAINER", raising=False)
        assert _missing_source_hint() is None

    def test_silent_when_something_is_mounted(self, monkeypatch, tmp_path) -> None:
        """A mistyped filename must not be blamed on a missing mount."""
        from veripp import cli

        monkeypatch.setenv("VERIPP_IN_CONTAINER", "1")
        populated = tmp_path / "src"
        populated.mkdir()
        (populated / "something.c").touch()
        monkeypatch.setattr(cli, "Path", lambda _: populated)
        assert cli._missing_source_hint() is None

    def test_fires_on_an_empty_mount_point(self, monkeypatch, tmp_path) -> None:
        from veripp import cli

        monkeypatch.setenv("VERIPP_IN_CONTAINER", "1")
        empty = tmp_path / "src"
        empty.mkdir()
        monkeypatch.setattr(cli, "Path", lambda _: empty)
        hint = cli._missing_source_hint()
        assert hint and "-v" in hint and "/src" in hint

    def test_explains_an_unreadable_mount_separately(self, monkeypatch, tmp_path) -> None:
        """Mounted-but-unreadable and nothing-mounted need different fixes.

        The image runs as uid 65534, so a project under a 0700 home directory
        is mounted yet unreadable. This is what broke the release workflow's
        smoke test on GitHub's runners, where mktemp hands back a 0700 dir.
        """
        from veripp import cli

        monkeypatch.setenv("VERIPP_IN_CONTAINER", "1")

        class Unreadable:
            def iterdir(self):
                raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(cli, "Path", lambda _: Unreadable())
        hint = cli._missing_source_hint()
        assert hint and "--user" in hint
        assert "empty" not in hint, "an unreadable mount is not an empty one"

    def test_the_image_sets_the_marker(self) -> None:
        assert "VERIPP_IN_CONTAINER=1" in read("Dockerfile")


class TestCveDemo:
    """The demo the README leads with, and a RELEASING pre-tag gate.

    It broke silently: the shim was C++-only (`#include <cstdlib>`, a bare
    `extern "C"`), which stopped compiling once the harness started following
    the source's language -- and stb_vorbis.c is C. Nothing in the suite ran
    the demo, so nothing noticed.
    """

    SHIM = "demo/cve-2019-13223/shim.hpp"

    def test_the_shim_compiles_as_c(self) -> None:
        # Judge the code, not the comments explaining it. Three tests in this
        # file have now been fooled by a comment quoting the thing they check.
        code = "\n".join(
            "" if line.lstrip().startswith(("//", "*", "/*")) else line
            for line in read(self.SHIM).splitlines()
        )

        assert "<cstdlib>" not in code, (
            "the harness for a .c translation unit is C; <cstdlib> does not "
            "exist there"
        )
        assert "<stdlib.h>" in code

        # A bare `extern "C"` is a syntax error in C, so it must be guarded.
        lines = code.splitlines()
        for line_no, line in enumerate(lines, 1):
            if 'extern "C"' in line:
                guard = "\n".join(lines[: line_no - 1])
                assert "__cplusplus" in guard, (
                    f'{self.SHIM}:{line_no} has an unguarded extern "C"'
                )

    @pytest.mark.esbmc
    def test_the_demo_still_finds_and_fixes_the_cve(self, tmp_path) -> None:
        import shutil
        import subprocess

        if shutil.which("git") is None:
            pytest.skip("git not on PATH")
        script = ROOT / "demo/cve-2019-13223/run.sh"
        result = subprocess.run(
            ["bash", str(script), str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if "git clone" in result.stderr and "fatal" in result.stderr:
            pytest.skip("demo needs to clone stb")
        output = result.stdout
        assert "Result: counterexample" in output, output[-2000:]
        assert "division by zero" in output, output[-2000:]
        assert "Result: verified" in output, output[-2000:]


class TestSystemHeadersAreReachable:
    """esbmc's bundled libc headers `#include_next` onto clang's resource
    headers. If those are absent, every translation unit that touches a real
    system header dies with "'stddef.h' file not found" -- and nothing else
    does, so an image that cannot parse a single line of real code passes a
    smoke test built from self-contained fixtures. That happened: the arm64
    image scored 13/13 while failing 104 of 117 functions in cJSON.
    """

    def test_the_arm64_stage_stages_clang_resource_headers(self) -> None:
        """Built against a system LLVM, esbmc hard-codes the path to clang's
        resource headers and its own libc headers #include_next onto them.
        The directory has to exist, as a real directory, at that path: a
        symlink does not satisfy clang's header search, and
        -DESBMC_CLANG_HEADERS_BUNDLED=ON does not substitute for it. Both were
        tried against a real cJSON scan before settling on this.
        """
        text = read("Dockerfile")
        arm_stage = text.split("AS esbmc-arm64", 1)[1].split("\nFROM ", 1)[0]
        assert "overlay" in arm_stage and "stddef.h" in arm_stage

    def test_the_runtime_applies_the_overlay(self) -> None:
        runtime = read("Dockerfile").split("AS runtime", 1)[1]
        assert "/opt/esbmc/overlay" in runtime and "cp -a" in runtime

    def test_the_smoke_test_exercises_system_headers(self) -> None:
        smoke = read("tests/image_smoketest.sh")
        for header in ("<stddef.h>", "<stdlib.h>", "<string.h>", "<cstddef>"):
            assert header in smoke, (
                f"the smoke test must include {header}: without a system "
                "header no fixture can detect a broken include chain"
            )
        assert "PARSING ERROR" in smoke, (
            "the smoke test must fail loudly on a frontend parse error rather "
            "than accept it as an ordinary non-zero exit"
        )


class TestSkillInstaller:
    """The skill can bootstrap veripp, but must not do it behind the user's
    back: every route is a large download or a source build."""

    PATH = "skills/veripp/install.sh"

    def test_is_executable_and_valid_bash(self) -> None:
        import subprocess

        assert (ROOT / self.PATH).stat().st_mode & stat.S_IXUSR
        result = subprocess.run(
            ["bash", "-n", str(ROOT / self.PATH)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_does_nothing_without_an_explicit_yes(self) -> None:
        text = read(self.PATH)
        assert "--yes" in text
        assert 'DO_IT=0' in text, "the default must be a dry report"

    def test_refuses_the_unsound_homebrew_formula(self) -> None:
        """`brew install esbmc` is 8.4, which carries esbmc#6508."""
        text = read(self.PATH)
        assert "brew install --HEAD esbmc" in text
        assert not re.search(r"^\s*plan \"brew install esbmc\"", text, re.M)

    def test_skill_tells_the_agent_to_ask_first(self) -> None:
        skill = read("skills/veripp/SKILL.md")
        assert "install.sh" in skill
        assert "before running" in skill and "--yes" in skill

    def test_installer_runs_dry_and_changes_nothing(self) -> None:
        import subprocess

        result = subprocess.run(
            ["bash", str(ROOT / self.PATH)],
            capture_output=True,
            text=True,
            timeout=300,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": "/tmp"},
        )
        assert result.returncode in (0, 3, 4), result.stdout + result.stderr
        assert "Re-run with --yes" in result.stdout or "image" in result.stdout


class TestActHarness:
    """Local Action testing on arm64, which is otherwise impossible: the
    action installs ESBMC by download, and upstream publishes no arm64 Linux
    binary, so it correctly refuses before anything can be exercised."""

    PATH = "tests/act_local.sh"

    def test_is_executable_and_valid_bash(self) -> None:
        import subprocess

        assert (ROOT / self.PATH).stat().st_mode & stat.S_IXUSR
        result = subprocess.run(
            ["bash", "-n", str(ROOT / self.PATH)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_resolves_libraries_rather_than_copying_symlinks(self) -> None:
        """`docker cp` copies a symlink as a symlink, so the staged libraries
        dangle and the binary will not start. cp -L inside the image is what
        makes this work at all."""
        assert "cp -L" in read(self.PATH)

    def test_stages_somewhere_the_runtime_shares(self) -> None:
        """colima and Lima do not share /tmp or /var/folders; a bind mount
        from there silently becomes an empty directory."""
        text = read(self.PATH)
        assert "$HOME" in text
        assert "/tmp/veripp-act" not in text

    def test_enumerates_only_keys_under_jobs(self) -> None:
        """A two-space-key grep across the whole file also matches `push:`
        under `on:`, and act then tries to run a job by that name. Scoping to
        the jobs: block fixes it without needing a YAML parser -- a local dev
        harness should not require a Python package to list four names."""
        # Judge the code, not the comment that explains it -- the fourth
        # time a test in this repo has been fooled by prose quoting the thing
        # it checks for.
        code = "\n".join(
            line for line in read(self.PATH).splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "/^jobs:/" in code, "job names must be read from the jobs: block"
        assert "import yaml" not in code and "yaml.safe_load" not in code, (
            "a local dev harness should not need PyYAML to list four job names"
        )

    def test_the_enumeration_actually_matches_the_workflow(self) -> None:
        import subprocess

        script = re.search(r"jobs=\$\(awk '(.*?)' \.github", read(self.PATH), re.S)
        assert script, "could not find the awk enumeration"
        result = subprocess.run(
            ["awk", script.group(1), ".github/workflows/action-selftest.yml"],
            capture_output=True, text=True, cwd=ROOT,
        )
        found = set(result.stdout.split())
        yaml = pytest.importorskip("yaml")
        expected = set(
            yaml.safe_load(read(".github/workflows/action-selftest.yml"))["jobs"]
        )
        assert found == expected, f"awk found {found}, workflow defines {expected}"

    def test_skips_the_job_it_structurally_cannot_run(self) -> None:
        """refuses-a-lying-checker installs its own broken checker at the very
        path the harness mounts a working one onto, read-only. It needs the
        absence of a checker; the harness exists to supply one."""
        text = read(self.PATH)
        assert "refuses-a-lying-checker" in text
        assert "INCOMPATIBLE" in text

    def test_relies_on_the_actions_reuse_step(self) -> None:
        """The harness only works because the action prefers an ESBMC already
        on PATH. If that step is ever dropped, this stops testing anything."""
        assert "Use an ESBMC already on PATH" in read("action.yml")


class TestChangelog:
    """The client-facing record of what changed. Useless if it drifts."""

    def test_documents_the_version_being_shipped(self) -> None:
        version = re.search(
            r'^version = "([^"]+)"', read("pyproject.toml"), re.M
        ).group(1)
        assert re.search(rf"^## {re.escape(version)}$", read("CHANGELOG.md"), re.M), (
            f"CHANGELOG.md has no section for {version}"
        )

    def test_versions_are_newest_first(self) -> None:
        found = re.findall(r"^## (\d+\.\d+\.\d+)$", read("CHANGELOG.md"), re.M)
        assert found, "no version sections"
        parsed = [tuple(int(p) for p in v.split(".")) for v in found]
        assert parsed == sorted(parsed, reverse=True), f"out of order: {found}"

    def test_says_what_a_verified_result_actually_means(self) -> None:
        """A changelog for a verification tool that never says "bounded"
        invites a reader to hear "verified" as "correct"."""
        text = read("CHANGELOG.md").lower()
        assert "bounded" in text and "doctor" in text

    def test_the_release_notes_extractor_matches_the_changelog(self) -> None:
        import subprocess
        import sys as _sys

        version = re.search(
            r'^version = "([^"]+)"', read("pyproject.toml"), re.M
        ).group(1)
        result = subprocess.run(
            [_sys.executable, str(ROOT / "scripts/release-notes.py"), version],
            capture_output=True, text=True, cwd=ROOT,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip(), "extracted empty release notes"
        assert result.stdout.strip() in read("CHANGELOG.md")

    def test_the_extractor_refuses_an_unknown_version(self) -> None:
        import subprocess
        import sys as _sys

        result = subprocess.run(
            [_sys.executable, str(ROOT / "scripts/release-notes.py"), "99.99.99"],
            capture_output=True, text=True, cwd=ROOT,
        )
        assert result.returncode != 0
        assert "no section" in result.stderr


class TestSkillBundledScript:
    """A skill's own files must be addressed by a path that resolves wherever
    the skill is installed -- personal, project, or plugin."""

    PATH = "skills/veripp/SKILL.md"

    def test_uses_the_documented_skill_dir_variable(self) -> None:
        """`./install.sh` only resolves if the agent's working directory is
        the skill directory, which it is not: it is the user's project."""
        text = read(self.PATH)
        assert "${CLAUDE_SKILL_DIR}/install.sh" in text
        code_lines = [
            line for line in text.splitlines()
            if line.strip().endswith("install.sh")
        ]
        for line in code_lines:
            assert "CLAUDE_SKILL_DIR" in line, f"unqualified path: {line!r}"

    def test_permits_the_dry_run_but_not_the_install(self) -> None:
        """The dry report changes nothing and should not need a prompt. The
        --yes form downloads hundreds of MB or starts a source build, and
        should. Allowing the bare path but not `*` draws exactly that line."""
        front = read(self.PATH).split("---", 2)[1]
        allowed = re.search(r"^allowed-tools: (.+)$", front, re.M)
        assert allowed, "no allowed-tools entry"
        rule = allowed.group(1).strip()
        assert rule == "Bash(${CLAUDE_SKILL_DIR}/install.sh)", rule
        assert "*" not in rule, (
            "a wildcard here would pre-authorise `--yes`, which is the one "
            "form that must be confirmed"
        )


class TestInstallerHonesty:
    """The installer reports what it observed, not what it guessed."""

    PATH = "skills/veripp/install.sh"

    def test_does_not_claim_an_image_is_unpublished(self) -> None:
        """Without credentials a private package is indistinguishable from a
        missing one, so "not published yet" is a guess presented as fact."""
        code = "\n".join(
            line for line in read(self.PATH).splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "not published yet" not in code
        assert "docker login" in code, (
            "the likely cause of a failed read is missing credentials; say so"
        )
