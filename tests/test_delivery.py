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
