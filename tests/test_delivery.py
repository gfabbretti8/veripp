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
        # `docker run --rm` and friends are not veripp flags.
        docker_flags = {"--rm", "--platform", "--entrypoint", "--user"}
        referenced = {
            flag
            for flag in re.findall(r"(?<![\w-])--[a-z][a-z0-9-]+", text)
            if flag not in docker_flags
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
