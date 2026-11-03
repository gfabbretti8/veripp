#!/usr/bin/env bash
# Assemble the ESBMC payload for a Linux wheel: the release's own static
# binary for one architecture, checked against the digest veripp pins and
# stripped. No image and no compiler are involved -- ESBMC 8.6 publishes a
# static binary for x86_64 and for arm64, and the wheel ships those bytes.
#
#   ./build_linux_payload.sh <x86_64|aarch64> <outdir>
#
# The release tag is ESBMC_RELEASE in src/veripp/checker.py (override with
# $ESBMC_VERSION); the digest is checker/<asset>.sha256, the same pin the
# workflows and the image check. `strip` has to understand the binary, so run
# this on a machine of the target architecture, or with multiarch binutils.
set -euo pipefail

ARCH="${1:?usage: build_linux_payload.sh <x86_64|aarch64> <outdir>}"
OUT="${2:-payload}"
HERE="$(cd "$(dirname "$0")" && pwd)"
RELEASE="${ESBMC_VERSION:-$(sed -n 's/^ESBMC_RELEASE = "\(.*\)"$/\1/p' "$HERE/../src/veripp/checker.py")}"
test -n "$RELEASE"

case "$ARCH" in
  x86_64|amd64)  ASSET=esbmc-linux.zip ;;
  aarch64|arm64) ASSET=esbmc-linux-armv8.zip ;;
  *) echo "no ESBMC release binary is published for $ARCH" >&2; exit 2 ;;
esac
PIN="$HERE/$ASSET.sha256"
test -s "$PIN" || { echo "no digest pinned for $ASSET at $PIN" >&2; exit 2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "fetching $ASSET from ESBMC $RELEASE"
curl -fsSL -o "$WORK/$ASSET" \
  "https://github.com/esbmc/esbmc/releases/download/$RELEASE/$ASSET"
# The bytes CI tested, and no others: a release asset never changes, so a
# mismatch means the download is not what it claims to be.
echo "$(cat "$PIN")  $WORK/$ASSET" | sha256sum -c -
unzip -q "$WORK/$ASSET" -d "$WORK/unz"
bin="$(find "$WORK/unz" -name esbmc -type f | head -1)"
test -n "$bin" || { echo "no esbmc binary inside $ASSET" >&2; exit 1; }

# Refuse to package a binary carrying a solver that cannot be redistributed.
# ESBMC's own COPYING flags MathSAT as academic/non-commercial and Yices as
# personal-use or GPL3. Z3, Boolector and Bitwuzla are all MIT, so a build
# carrying only those is fine to ship. Which solvers a given binary actually
# contains is not obvious by inspection, so this checks rather than trusts.
# Test for the solver's *API symbols*, not its name: ESBMC names every
# solver it knows about in its own option handling and help text, so a plain
# grep for "yices" matches a build that does not contain a byte of it.
FORBIDDEN=""
for probe in "mathsat:msat_" "yices:yices_"; do
  solver="${probe%%:*}"; prefix="${probe##*:}"
  # grep exits 1 on no match, which is the good case; keep pipefail quiet.
  count=$({ grep -ao "${prefix}[a-z_]*" "$bin" || true; } | sort -u | wc -l)
  if [ "${count:-0}" -gt 0 ]; then
    FORBIDDEN="$FORBIDDEN $solver"
  fi
done
if [ -n "$FORBIDDEN" ]; then
  echo "refusing to package this checker: it contains$FORBIDDEN," >&2
  echo "which ESBMC's COPYING says may not be redistributed freely." >&2
  exit 1
fi

rm -rf "$OUT"
mkdir -p "$OUT/bin" "$OUT/lib" "$OUT/licenses"
cp "$bin" "$OUT/bin/esbmc"
chmod +x "$OUT/bin/esbmc"
# The release ships its own notices beside the binary (ESBMC's COPYING, and
# Boost, Boolector and Z3); they travel inside the package too.
if [ -d "$(dirname "$bin")/../license" ]; then
  cp "$(dirname "$bin")"/../license/* "$OUT/licenses/"
fi
# Static and unstripped: ~700MB on x86_64, most of it debug symbols. The
# stripped binary is what the image ships too, and `veripp doctor` is run on
# the installed wheel before anything is published.
strip --strip-unneeded "$OUT/bin/esbmc"
# A statically linked binary needs nothing from the host; say so, since the
# workflow derives the manylinux tag from the glibc floor.
if ! objdump -p "$OUT/bin/esbmc" 2>/dev/null | grep -q NEEDED; then
  echo "note: statically linked -- no bundled libraries, no glibc floor"
fi
echo "payload: $OUT ($(du -sh "$OUT" | cut -f1))"
