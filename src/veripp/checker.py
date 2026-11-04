"""Getting a sound ESBMC onto this machine, without a scavenger hunt.

The checker is a C++ binary, so pip cannot install it, and the one release
users reach for first (8.4) silently misses out-of-bounds writes to a member
array indexed by another member -- esbmc#6508, the ordinary container idiom.
So "install ESBMC" is really "install a build that is not 8.4", which is a
sentence nobody should have to learn before their first verification.

This module makes that one command for the platforms where a relocatable
prebuilt exists, and says so plainly where none does. Nothing is accepted
until it has passed the same soundness probes `veripp doctor` runs: a
downloaded checker that cannot detect a planted bug is worse than no checker,
because every result built on it is a false proof.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

#: The ESBMC release veripp installs, and the one every workflow and the
#: container image pin. 8.4 is the floor nobody should go below (esbmc#6508);
#: 8.5 and later carry the fix, and from 8.6 upstream publishes a static
#: Linux binary for arm64 as well as x86_64. A release's assets are immutable,
#: unlike the `weekly` tag, which is re-cut in place under one name -- so a
#: digest pinned here stays true.
ESBMC_RELEASE = "v8.6"
RELEASE_URL = f"https://github.com/esbmc/esbmc/releases/download/{ESBMC_RELEASE}"

#: SHA-256 of the Linux assets of that release. `install` refuses a download
#: that does not match: the only way those bytes change is someone replacing
#: the asset, and a checker is the one program nobody should take on trust.
#: checker/*.sha256 hold the same digests for the workflows and the image; a
#: test keeps the two in step.
PINNED_SHA256 = {
    "esbmc-linux.zip":
        "07b00b4a2590fe285edb5ce063ca2e5fbb368bbe556cfe139014c173717637f6",
    "esbmc-linux-armv8.zip":
        "d3e9fbdeba2ba6cfeb54b32c8fb8a9cd8125f6c5f92fbf05a212a4c2289fcc12",
}


@dataclass(frozen=True)
class Source:
    """Where this platform's checker comes from, or why it cannot."""

    url: str | None
    #: Set when no relocatable prebuilt exists: what to do instead.
    unavailable_reason: str | None = None
    binary_name: str = "esbmc"
    #: The digest the download must have, where the release is pinned.
    sha256: str | None = None

    @property
    def available(self) -> bool:
        return self.url is not None


def managed_dir() -> Path:
    """Where veripp keeps a checker it installed itself.

    Deliberately not inside the package: a wheel is replaced wholesale on
    upgrade, and re-downloading a 100MB binary on every `pip install -U`
    would be a poor trade for tidiness.
    """
    if override := os.environ.get("VERIPP_CHECKER_DIR"):
        return Path(override).expanduser()
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "veripp" / "checker"


def state_dir() -> Path:
    """Where veripp remembers things about checkers: their digests, and what
    the soundness probes found. Beside the managed checker, and moved with it
    by $VERIPP_CHECKER_DIR."""
    return managed_dir().parent


def managed_esbmc() -> str | None:
    """The checker veripp installed, if there is one and it can be executed."""
    candidate = managed_dir() / "bin" / "esbmc"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def source_for(system: str | None = None, machine: str | None = None) -> Source:
    """The download for this platform, or the reason there is not one.

    Linux gets the release's static binary for its architecture: upstream
    publishes x86_64 and, since 8.6, arm64 (`esbmc-linux-armv8.zip`). Before
    that the only prebuilt arm64 Linux ESBMC was the Homebrew bottle at the
    unsound 8.4, and this refused rather than install it.
    """
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()

    if system == "Linux" and machine in ("x86_64", "amd64"):
        return _release("esbmc-linux.zip")
    if system == "Linux" and machine in ("aarch64", "arm64", "armv8", "armv8l"):
        return _release("esbmc-linux-armv8.zip")
    if system == "Windows":
        # Not a platform veripp supports: the CLI is not tested there and
        # nothing is packaged for it. WSL is Linux x86_64 or arm64, and gets
        # the Linux route.
        return Source(
            url=None,
            unavailable_reason=(
                "veripp does not support Windows. Run it under WSL, where the "
                "Linux checker is bundled with `pip install veripp`, or use "
                'the container: docker run --rm -v "$PWD:/src" '
                "ghcr.io/gfabbretti8/veripp scan src/parser.c"
            ),
        )
    if system == "Darwin":
        # The macOS release zip links against Homebrew's z3/gmp/mpfr by
        # absolute path, so it is not relocatable: unzipping it elsewhere
        # produces a binary that will not start.
        return Source(
            url=None,
            unavailable_reason=(
                "ESBMC publishes no relocatable macOS build (the release zip "
                "links against Homebrew's z3/gmp/mpfr by absolute path).\n"
                "  install it with:  brew install --HEAD esbmc\n"
                "  NOT `brew install esbmc`, which is 8.4 and unsound for "
                "member-array writes (esbmc#6508)."
            ),
        )
    return Source(
        url=None,
        unavailable_reason=(
            f"no prebuilt ESBMC is published for {system}/{machine}.\n"
            "  use the container, which carries one for x86_64 and arm64:\n"
            '    docker run --rm -v "$PWD:/src" ghcr.io/gfabbretti8/veripp '
            "scan src/parser.c"
        ),
    )


def _release(asset: str, binary_name: str = "esbmc") -> Source:
    return Source(
        url=f"{RELEASE_URL}/{asset}",
        binary_name=binary_name,
        sha256=PINNED_SHA256.get(asset),
    )


def _safe_extract(archive: Path, dest: Path) -> None:
    """Unzip, refusing any member that would land outside `dest`.

    A zip may name `../../etc/whatever`; extracting it as given writes
    wherever the path leads. This is a download from the internet being
    unpacked with the user's own permissions, so the guard is not optional.
    """
    dest.mkdir(parents=True, exist_ok=True)
    resolved_dest = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            target = (dest / member).resolve()
            if not target.is_relative_to(resolved_dest):
                raise RuntimeError(
                    f"refusing archive: member {member!r} would extract "
                    "outside the install directory"
                )
        zf.extractall(dest)


def _locate_binary(root: Path, binary_name: str) -> Path | None:
    """Find the checker inside an extracted release, whatever it nests it in.

    The layout has changed between releases (`esbmc`, `bin/esbmc`,
    `esbmc-linux/bin/esbmc`), and pinning one means a silent break the next
    time it moves.
    """
    direct = root / binary_name
    if direct.is_file():
        return direct
    matches = sorted(root.rglob(binary_name))
    return matches[0] if matches else None


def download(url: str, dest: Path, opener=urllib.request.urlopen) -> str:
    """Fetch `url` to `dest`, returning the SHA-256 of what arrived.

    The caller compares it with the pin where there is one, and records it
    either way: what was installed is the question asked after the fact.
    """
    digest = hashlib.sha256()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with opener(url) as response, dest.open("wb") as out:
        while chunk := response.read(1 << 16):
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


@dataclass
class InstallResult:
    path: str | None
    sha256: str = ""
    probes: dict[str, bool] | None = None
    #: Set when nothing was installed; already phrased for a user to act on.
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.path is not None


def install(
    dest: Path | None = None,
    source: Source | None = None,
    opener=urllib.request.urlopen,
    progress=None,
) -> InstallResult:
    """Download a checker for this platform and keep it only if it is sound.

    The probe is the point of this function, not a formality. An unsound
    checker answers "verified" on programs that provably fail, so installing
    one without checking would hand the user false proofs with veripp's name
    on them. A binary that fails the probe is deleted, not merely reported.
    """
    from .esbmc import check_soundness

    source = source or source_for()
    if not source.available:
        return InstallResult(path=None, error=source.unavailable_reason)

    dest = dest or managed_dir()
    staging = dest / "download"
    archive = staging / "esbmc.zip"
    if progress:
        progress(f"downloading {source.url}")
    try:
        digest = download(source.url, archive, opener=opener)
    except Exception as exc:  # network, DNS, HTTP -- all the same to the user
        return InstallResult(path=None, error=f"could not download {source.url}: {exc}")
    if source.sha256 and digest != source.sha256:
        # A release asset does not change. One that has is not the checker
        # this veripp was tested with, whatever else it may be.
        shutil.rmtree(staging, ignore_errors=True)
        return InstallResult(
            path=None,
            sha256=digest,
            error=(
                f"{source.url} does not match the digest veripp pins for it:\n"
                f"  expected {source.sha256}\n"
                f"  got      {digest}\n"
                "  Nothing was installed. If upstream really re-published the "
                "asset, the pin in veripp needs a reviewed bump."
            ),
        )

    unpacked = staging / "unpacked"
    shutil.rmtree(unpacked, ignore_errors=True)
    try:
        _safe_extract(archive, unpacked)
    except (zipfile.BadZipFile, RuntimeError) as exc:
        return InstallResult(path=None, error=str(exc))

    binary = _locate_binary(unpacked, source.binary_name)
    if binary is None:
        return InstallResult(
            path=None,
            error=f"no {source.binary_name} found inside {source.url}",
        )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    if progress:
        progress("checking that it detects a planted bug")
    try:
        probes = check_soundness(str(binary))
    except Exception as exc:
        shutil.rmtree(unpacked, ignore_errors=True)
        return InstallResult(
            path=None, error=f"the downloaded checker could not be run: {exc}"
        )
    if not all(probes.values()):
        missed = ", ".join(name for name, ok in probes.items() if not ok)
        shutil.rmtree(unpacked, ignore_errors=True)
        return InstallResult(
            path=None,
            probes=probes,
            error=(
                f"the downloaded checker MISSES: {missed}.\n"
                "  It was deleted rather than installed: a checker that "
                "verifies a program that provably fails turns every result "
                "built on it into a false proof."
            ),
        )

    # Only now is it allowed to become the checker veripp reaches for. Moving
    # the whole release keeps the binary next to the libraries it may need.
    final_bin = dest / "bin" / source.binary_name
    shutil.rmtree(dest / "bin", ignore_errors=True)
    (dest / "bin").mkdir(parents=True, exist_ok=True)
    for item in binary.parent.iterdir():
        shutil.move(str(item), dest / "bin" / item.name)
    final_bin.chmod(final_bin.stat().st_mode | stat.S_IXUSR)
    shutil.rmtree(staging, ignore_errors=True)
    record = f"url: {source.url}\nsha256: {digest}\n"
    (dest / "INSTALLED").write_text(record, encoding="utf-8")
    return InstallResult(path=str(final_bin), sha256=digest, probes=probes)


DOCKER_HINT = (
    'docker run --rm -v "$PWD:/src" ghcr.io/gfabbretti8/veripp scan FILE.c'
)


def install_hint() -> str:
    """The exact command for *this* machine, not a link to go read.

    Architecture still matters: the Linux release comes as one binary per
    architecture, and handing an arm64 user the x86_64 URL gets them a
    download that will not execute. Where veripp can fetch and probe the
    right one itself -- Linux x86_64 and arm64 -- that is the whole
    instruction.
    """
    # One command, and it refuses to keep a download that fails the probes.
    if source_for().available:
        return "veripp install-checker"

    system = platform.system()
    machine = platform.machine().lower()

    if system == "Darwin":
        # The macOS release zip links against Homebrew's z3/gmp/mpfr by
        # absolute path, so it is not relocatable; brew is the only sane route.
        return "brew install --HEAD esbmc"

    if system == "Linux":
        return (
            f"{DOCKER_HINT}\n"
            f"      (no prebuilt ESBMC is published for Linux/{machine}; the image "
            "is built for x86_64 and arm64)"
        )

    return DOCKER_HINT


def missing_checker() -> str:
    """What to say when there is no checker at all, and how to get one.

    veripp looks in four places, not only on PATH, and saying "not found on
    PATH" sends people to fix the one place that may already be fine.
    """
    return (
        "no ESBMC checker found: $VERIPP_ESBMC is not set, none was installed "
        "by `veripp install-checker`, there is none on PATH, and this veripp "
        "has none bundled.\n"
        f"  install one with:  {install_hint()}"
    )


#: Where a platform wheel of veripp carries its checker. The pure wheel, the
#: sdist and a source checkout have no such directory, and on those
#: `install-checker` or an esbmc on PATH is the route.
BUNDLED_DIR = Path(__file__).resolve().parent / "bin"


def bundled_esbmc(bundled_dir: Path | None = None) -> str | None:
    """The checker that came inside this veripp wheel, if this is one.

    Linux x86_64 and arm64 wheels ship ESBMC's static release binary under
    `veripp/bin/`, put there by checker/bundle_wheel.py from the same pinned
    download `install-checker` makes. Every other install has none here, and
    veripp stays usable on them: `doctor` says what to run.
    """
    directory = bundled_dir or BUNDLED_DIR
    candidate = directory / "esbmc"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None
