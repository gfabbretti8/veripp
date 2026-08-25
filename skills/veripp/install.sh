#!/usr/bin/env bash
# Work out how to get a working veripp on THIS machine, and say what it costs.
#
# Default is a dry report: it changes nothing. That is deliberate. Every route
# here is either a large download or a source build, and an agent should put
# the number in front of the user before spending their bandwidth, their disk,
# or an hour of their CPU. Re-run with --yes to actually do it.
#
#   ./install.sh          # report the plan
#   ./install.sh --yes    # carry it out
set -uo pipefail

DO_IT=0
[ "${1:-}" = "--yes" ] && DO_IT=1

say()  { printf '%s\n' "$*"; }
plan() { printf '  %s\n' "$*"; }

# --- already working? ------------------------------------------------------
if command -v veripp >/dev/null 2>&1; then
  if veripp doctor >/dev/null 2>&1; then
    say "veripp is installed and its checker passes the soundness probe."
    say "Nothing to do."
    exit 0
  fi
  say "veripp is installed but 'veripp doctor' is not happy."
  say "Run 'veripp doctor' and read what it says -- most likely the ESBMC on"
  say "PATH is the 8.4 release, which silently misses out-of-bounds writes"
  say "(esbmc/esbmc#6508). Proofs from it are not worth having."
  exit 3
fi

os="$(uname -s)"; arch="$(uname -m)"
say "No veripp on PATH. Host: $os/$arch"
say ""

# --- the container needs no install at all ---------------------------------
IMAGE="${VERIPP_IMAGE:-ghcr.io/gfabbretti8/veripp}"
if command -v docker >/dev/null 2>&1; then
  if docker manifest inspect "$IMAGE" >/dev/null 2>&1; then
    say "Recommended: use the image. It installs nothing and carries an ESBMC"
    say "that passed the soundness probe when the image was built."
    plan "docker run --rm -v \"\$PWD:/src\" $IMAGE scan FILE.c"
    say ""
    say "Pull is roughly 450-540 MB depending on architecture."
    [ "$DO_IT" = "1" ] && { say "Pulling..."; docker pull "$IMAGE"; exit $?; }
    exit 0
  fi
  # A failed manifest read means "cannot see it", not "does not exist": a
  # private package looks identical to a missing one without credentials.
  say "note: could not read $IMAGE. It may be private (try"
  say "      'docker login ghcr.io'), or not published for your platform."
  say "      Falling back to installing on the host."
  say ""
fi

# --- otherwise install veripp itself, then ESBMC ---------------------------
say "Installing on the host needs two separate things:"
say ""
say "1. veripp (a Python package)"
if command -v uv >/dev/null 2>&1; then
  plan "uv tool install git+https://github.com/gfabbretti8/veripp"
  say "   (not on PyPI yet; installs from the repository, which you need"
  say "    read access to)"
else
  plan "install uv first: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi
say ""
say "2. ESBMC (a C++ binary -- uv cannot install this one)"
case "$os:$arch" in
  Darwin:*)
    plan "brew install --HEAD esbmc"
    say "   NOT 'brew install esbmc': that is 8.4, which carries esbmc#6508."
    say "   --HEAD builds from source. Budget tens of minutes, and it pulls"
    say "   LLVM if you do not have it."
    ;;
  Linux:x86_64|Linux:amd64)
    plan "curl -fsSL -o /tmp/esbmc.zip https://github.com/esbmc/esbmc/releases/download/weekly/esbmc-linux.zip"
    plan "unzip -q /tmp/esbmc.zip -d ~/.local/esbmc && chmod +x ~/.local/esbmc/*/bin/esbmc"
    say "   235 MB download. Put the binary on PATH afterwards."
    ;;
  Linux:aarch64|Linux:arm64)
    say "   No prebuilt ESBMC exists for Linux/arm64, and the only prebuilt"
    say "   arm64 build anywhere (the Homebrew 8.4 bottle) is unsound."
    say "   Use the image, or build ESBMC from source -- see this project's"
    say "   Dockerfile for a working cmake configuration."
    exit 4
    ;;
  *)
    say "   Unsupported host. Use the image."
    exit 4
    ;;
esac
say ""
say "Re-run with --yes to carry this out, or run the commands yourself."
[ "$DO_IT" = "1" ] || exit 0

say "Proceeding."
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install "git+https://github.com/gfabbretti8/veripp" || exit 1
case "$os" in
  Darwin) brew install --HEAD esbmc ;;
  Linux)
    curl -fsSL -o /tmp/esbmc.zip https://github.com/esbmc/esbmc/releases/download/weekly/esbmc-linux.zip \
      && mkdir -p ~/.local/esbmc && unzip -qo /tmp/esbmc.zip -d ~/.local/esbmc \
      && chmod +x ~/.local/esbmc/*/bin/esbmc
    say "Add this to PATH: $(dirname "$(find ~/.local/esbmc -name esbmc -type f | head -1)")"
    ;;
esac
veripp doctor
