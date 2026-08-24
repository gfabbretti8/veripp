#!/usr/bin/env bash
# Exercise a built veripp image the way a user actually would.
#
# This runs against the image that is about to be published, on the
# architecture it was built for. It is deliberately end-to-end: the point is
# not that the binary starts, it is that a proof and a counterexample both
# come out correct, from a read-only bind mount, as a non-root user.
#
# Keep at least one fixture that includes real system headers. An earlier
# version of this file tested only self-contained toys, and passed 13/13 on an
# arm64 image that could not parse a single line of real code -- 104 of
# cJSON's 117 functions came back as parse errors. Self-contained fixtures
# cannot see a broken include chain, because they never use it.
set -uo pipefail

IMAGE="${1:?usage: image_smoketest.sh IMAGE[:TAG]}"
DOCKER="${DOCKER:-docker}"

# The scratch directory has to be visible to the container runtime. In CI that
# is any temp dir, but on a macOS VM runtime (colima, Lima, Docker Desktop with
# a restricted file-sharing list) /var/folders -- where macOS mktemp puts you --
# is typically not shared into the VM, and the mount silently comes up empty.
# SMOKE_TMPDIR lets the caller point somewhere the runtime can see.
work="$(mktemp -d "${SMOKE_TMPDIR:-${TMPDIR:-/tmp}}/veripp-smoke.XXXXXX")"
trap 'rm -rf "$work"' EXIT
# mktemp gives 0700, and the image runs as uid 65534, so without this the
# container cannot read its own mount. Real users hit the same thing when
# their project sits under a restrictive home directory; the fix for them is
# --user "$(id -u):$(id -g)", which the README documents.
chmod 755 "$work"

# Set when testing an image whose architecture is not the host's, to stop the
# runtime emitting a platform-mismatch warning into every captured output.
PLATFORM_ARG=()
[ -n "${PLATFORM:-}" ] && PLATFORM_ARG=(--platform "$PLATFORM")

pass=0 fail=0
check() { # check NAME EXPECTED_RC ACTUAL_RC [detail]
  if [ "$2" = "$3" ]; then
    printf '  ok   %s\n' "$1"; pass=$((pass+1))
  else
    printf '  FAIL %s (expected rc=%s, got rc=%s)\n%s\n' "$1" "$2" "$3" "${4:-}"; fail=$((fail+1))
  fi
}

run() { $DOCKER run --rm "${PLATFORM_ARG[@]}" -v "$work:/src:ro" "$IMAGE" "$@" 2>&1; }

cat > "$work/safe.c" <<'EOF'
int clamp(int x) {
  if (x < 0) return 0;
  if (x > 100) return 100;
  return x;
}
EOF

cat > "$work/buggy.c" <<'EOF'
int mean(int a, int b) { return (a + b) / 2; }
EOF

cat > "$work/oob.c" <<'EOF'
int pick(int i) {
  int t[4] = {1, 2, 3, 4};
  return t[i];
}
EOF

# Every fixture above is self-contained, and that is exactly how an image that
# could not parse ANY real code once passed this test 13/13: esbmc's bundled
# libc headers #include_next onto clang's resource headers, and if those are
# missing the failure only appears once a translation unit reaches a system
# header. Which is all real code, and none of the toys.
cat > "$work/headers.c" <<'EOF'
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

size_t span(const char *s) {
  if (s == NULL) return 0;
  return strlen(s);
}
EOF

cat > "$work/headers.cpp" <<'EOF'
#include <cstddef>
#include <cstring>

std::size_t span_cpp(const char *s) {
  if (s == nullptr) return 0;
  return std::strlen(s);
}
EOF

echo "== $IMAGE ($($DOCKER image inspect -f '{{.Architecture}}' "$IMAGE" 2>/dev/null || echo '?')) =="

# Fail loudly and specifically if the bind mount did not make it into the
# container, instead of reporting eight confusing "file not found" failures.
if ! run --help >/dev/null 2>&1; then
  echo "FATAL: cannot run $IMAGE at all"; exit 1
fi
seen=$($DOCKER run --rm "${PLATFORM_ARG[@]}" -v "$work:/src:ro" --entrypoint sh "$IMAGE" -c 'ls /src' 2>&1)
case "$seen" in
  *safe.c*) ;;
  *"Permission denied"*)
     echo "FATAL: /src is mounted but the container user cannot read it."
     echo "       $work is mode $(stat -c '%a' "$work" 2>/dev/null || stat -f '%Lp' "$work")"
     echo "       and the image runs as a non-root uid. chmod it, or pass"
     echo '       --user "$(id -u):$(id -g)".'
     echo "       ls /src gave: $seen"
     exit 1 ;;
  *) echo "FATAL: the bind mount of $work is empty inside the container."
     echo "       Your container runtime is not sharing that path."
     echo "       Re-run with SMOKE_TMPDIR=\$HOME/tmp (and mkdir it first)."
     echo "       ls /src gave: $seen"
     exit 1 ;;
esac

out=$(run --help); check "--help" 0 $? "$out"
case "$out" in *verify*scan*doctor*) ;; *) echo "  FAIL --help missing subcommands"; fail=$((fail+1)) ;; esac

# The image must ship a checker that can find planted bugs. If doctor fails,
# every "verified" the image ever prints is worthless, so nothing else matters.
out=$(run doctor); check "doctor (checker present and sound)" 0 $? "$out"

out=$(run verify safe.c --function clamp)
check "verify a correct function -> VERIFIED (rc 0)" 0 $? "$out"

# (a + b) overflows before the divide.
out=$(run verify buggy.c --function mean)
check "verify an overflowing function -> COUNTEREXAMPLE (rc 1)" 1 $? "$out"
case "$out" in *[Oo]verflow*) ;; *) echo "  FAIL counterexample did not mention overflow"; fail=$((fail+1)) ;; esac

out=$(run verify oob.c --function pick)
check "out-of-bounds read -> COUNTEREXAMPLE (rc 1)" 1 $? "$out"

# The check that actually represents real code.
out=$(run verify headers.c --function span)
rc=$?
case "$out" in
  *"file not found"*|*"PARSING ERROR"*)
    printf '  FAIL C system headers do not resolve:\n%s\n' "$out"; fail=$((fail+1)) ;;
  *) check "a C file including <stddef.h>/<stdlib.h>/<string.h>" 0 "$rc" "$out" ;;
esac

out=$(run verify headers.cpp --function span_cpp)
rc=$?
case "$out" in
  *"file not found"*|*"PARSING ERROR"*)
    printf '  FAIL C++ system headers do not resolve:\n%s\n' "$out"; fail=$((fail+1)) ;;
  *) check "a C++ file including <cstddef>/<cstring>" 0 "$rc" "$out" ;;
esac

out=$(run scan safe.c)
check "scan a file" 0 $? "$out"

out=$(run harness safe.c --function clamp)
check "harness prints without verifying" 0 $? "$out"
case "$out" in
  *VERIPP_NONDET*) printf '  ok   harness drives the function with nondet inputs\n'; pass=$((pass+1)) ;;
  *) echo "  FAIL harness has no VERIPP_NONDET_* inputs"; fail=$((fail+1)) ;;
esac

out=$(run scan safe.c --json)
if printf '%s' "$out" | head -c1 | grep -q '[{[]'; then
  printf '  ok   --json emits JSON on stdout\n'; pass=$((pass+1))
else
  printf '  FAIL --json did not emit JSON:\n%s\n' "$out"; fail=$((fail+1))
fi

# A user error must be a clean rc=2, not a traceback.
out=$(run verify safe.c --function no_such_function)
check "unknown function -> usage error (rc 2)" 2 $? "$out"
case "$out" in *Traceback*) echo "  FAIL python traceback leaked to the user"; fail=$((fail+1)) ;; esac

# The container must not need to be root, and must not need write access to
# the mounted source.
out=$($DOCKER run --rm "${PLATFORM_ARG[@]}" -v "$work:/src:ro" "$IMAGE" doctor 2>&1)
check "runs with a read-only source mount" 0 $? "$out"

id_out=$($DOCKER run --rm "${PLATFORM_ARG[@]}" --entrypoint id "$IMAGE" -u 2>/dev/null | tail -1)
if [ "$id_out" = "0" ]; then
  echo "  FAIL image runs as root"; fail=$((fail+1))
else
  printf '  ok   runs as non-root (uid %s)\n' "$id_out"; pass=$((pass+1))
fi

# Size is a feature. The published esbmc binary is 604MB unstripped and the
# runtime once carried a compiler it never invoked; both were found only by
# looking. A ceiling turns the next regression into a test failure.
LIMIT_MB="${SMOKE_MAX_MB:-700}"
# Measured from inside, not via `docker image inspect -f {{.Size}}`. For an
# image buildx loaded for a non-native platform, that field under-reports
# badly -- it said 162 MB for a filesystem that is really 446 MB -- and a size
# guard that reads low is worse than none.
size_mb=$($DOCKER run --rm "${PLATFORM_ARG[@]}" --entrypoint sh "$IMAGE" \
          -c "du -sm --exclude=/proc --exclude=/sys / 2>/dev/null | tail -1 | cut -f1")
if [ "$size_mb" -le "$LIMIT_MB" ]; then
  printf '  ok   image is %s MB (ceiling %s MB)\n' "$size_mb" "$LIMIT_MB"; pass=$((pass+1))
else
  printf '  FAIL image is %s MB, over the %s MB ceiling\n' "$size_mb" "$LIMIT_MB"; fail=$((fail+1))
fi

echo "-- $pass passed, $fail failed"
[ "$fail" -eq 0 ]
