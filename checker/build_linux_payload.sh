#!/usr/bin/env bash
# Assemble a relocatable ESBMC payload for a Linux wheel.
#
# The binary comes from veripp's own image, which builds ESBMC from source
# with Z3 only -- no Boolector, Bitwuzla, MathSAT or Yices. That is not an
# optimisation: ESBMC's COPYING notes that MathSAT is academic/non-commercial
# and Yices is personal-use or GPL3, so the fat official release is the one
# that cannot be redistributed. The slim build is both smaller and clean.
#
#   ./build_linux_payload.sh <image> <outdir>
set -euo pipefail

IMAGE="${1:-ghcr.io/gfabbretti8/veripp}"
OUT="${2:-payload}"
CTX="$(mktemp -d)"
trap 'rm -rf "$CTX"' EXIT

cat > "$CTX/Dockerfile" <<DOCKERFILE
FROM ${IMAGE} AS src
USER root
RUN set -eux; \\
    o=/payload; mkdir -p \$o/bin \$o/lib; \\
    cp "\$(command -v esbmc)" \$o/bin/esbmc; \\
    ldd \$o/bin/esbmc 2>/dev/null | awk '{print \$3}' | grep '^/' | sort -u | while read -r f; do \\
      case "\$f" in */libc.so.*|*/libm.so.*|*/libpthread*|*/libdl*|*/librt*|*/ld-linux*) continue;; esac; \\
      cp -L "\$f" \$o/lib/; \\
    done

FROM debian:trixie-slim
RUN apt-get update -qq && apt-get install -y -qq patchelf binutils && rm -rf /var/lib/apt/lists/*
COPY --from=src /payload /payload
# The image relies on its own library path; a wheel cannot. \$ORIGIN makes the
# tree work wherever pip unpacks it.
RUN set -eux; \\
    patchelf --set-rpath '\$ORIGIN/../lib' /payload/bin/esbmc; \\
    for f in /payload/lib/*.so*; do patchelf --set-rpath '\$ORIGIN' "\$f" || true; done; \\
    cp -r /payload /relocated && /relocated/bin/esbmc --version
DOCKERFILE

docker build -q -t veripp-checker-payload "$CTX" >/dev/null

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
  count=$(docker run --rm --entrypoint sh veripp-checker-payload \
    -c "grep -ao '${prefix}[a-z_]*' /payload/bin/esbmc 2>/dev/null | sort -u | wc -l")
  if [ "${count:-0}" -gt 0 ]; then
    FORBIDDEN="$FORBIDDEN $solver"
  fi
done
if [ -n "$FORBIDDEN" ]; then
  echo "refusing to package this checker: it contains$FORBIDDEN," >&2
  echo "which ESBMC's COPYING says may not be redistributed freely." >&2
  echo "Use a build from source with -DENABLE_Z3=ON and every other solver" >&2
  echo "OFF -- see the arm64 stage of this repository's Dockerfile." >&2
  exit 1
fi
cid="$(docker create veripp-checker-payload)"
rm -rf "$OUT"; mkdir -p "$OUT"
docker cp "$cid:/payload/." "$OUT/"
docker rm "$cid" >/dev/null

if [ -z "$(ls -A "$OUT/lib" 2>/dev/null)" ]; then
  echo "note: no bundled libraries -- this checker is statically linked"
fi
echo "payload: $OUT ($(du -sh "$OUT" | cut -f1))"
echo "glibc floor: $(docker run --rm --entrypoint sh veripp-checker-payload -c \
  'for f in /payload/bin/esbmc /payload/lib/*.so*; do objdump -T "$f" 2>/dev/null | grep -o "GLIBC_[0-9.]*"; done | sort -V -u | tail -1')"
