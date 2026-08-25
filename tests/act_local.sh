#!/usr/bin/env bash
# Run the GitHub Action locally with act, including on arm64.
#
# The obstacle: the action installs ESBMC by downloading it, and upstream
# publishes no arm64 Linux binary -- so on an Apple Silicon machine the action
# correctly refuses before it can be tested. But our own arm64 image contains
# a working checker, and the act runner is the same Ubuntu 24.04 / glibc 2.39,
# so the binary can simply be lifted out and mounted in. The action's "reuse an
# ESBMC already on PATH" step then finds it and skips the download.
#
#   ./tests/act_local.sh                      # the whole self-test workflow
#   ./tests/act_local.sh proves-a-good-one    # one job
#
# Needs: act, docker, and a locally built veripp image (IMAGE below).
set -euo pipefail

IMAGE="${VERIPP_IMAGE:-veripp:arm64-test}"
PLATFORM="${ACT_PLATFORM:-linux/arm64}"
JOB="${1:-}"
# Must live somewhere the container runtime shares. On colima/Lima that rules
# out /tmp and /var/folders; $HOME is shared.
STAGE="${ACT_STAGE:-$HOME/.cache/veripp-act}"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "no local image '$IMAGE'. Build one first:" >&2
  echo "  docker buildx build --platform $PLATFORM --load -t $IMAGE ." >&2
  exit 1
fi

if [ ! -x "$STAGE/bin/esbmc" ]; then
  echo "== staging a checker out of $IMAGE =="
  rm -rf "$STAGE"; mkdir -p "$STAGE/opt-esbmc/lib" "$STAGE/bin"

  cid=$(docker create --platform "$PLATFORM" "$IMAGE")
  trap 'docker rm "$cid" >/dev/null 2>&1 || true' EXIT
  docker cp "$cid:/opt/esbmc/bin" "$STAGE/opt-esbmc/bin" >/dev/null
  docker cp "$cid:/usr/lib/llvm-22" "$STAGE/llvm-22" >/dev/null
  docker rm "$cid" >/dev/null; trap - EXIT

  # Resolve the shared libraries inside the image: `docker cp` copies symlinks
  # unresolved, which yields a pile of dangling links and a binary that will
  # not start.
  docker run --rm --platform "$PLATFORM" --entrypoint bash "$IMAGE" -c '
    mkdir -p /tmp/libs
    for l in $(LD_LIBRARY_PATH=/opt/esbmc/lib ldd /opt/esbmc/bin/esbmc | awk "/=>/ {print \$3}"); do
      cp -L "$l" /tmp/libs/ 2>/dev/null
    done
    tar -cf - -C /tmp/libs .' | tar -xf - -C "$STAGE/opt-esbmc/lib"

  cat > "$STAGE/bin/esbmc" <<'WRAP'
#!/bin/sh
export LD_LIBRARY_PATH=/opt/esbmc/lib
exec /opt/esbmc/bin/esbmc "$@"
WRAP
  chmod +x "$STAGE/bin/esbmc"
fi

opts=(
  -v "$STAGE/opt-esbmc:/opt/esbmc:ro"
  -v "$STAGE/llvm-22:/usr/lib/llvm-22:ro"
  -v "$STAGE/bin/esbmc:/usr/local/bin/esbmc:ro"
  # act's runner has had DNS trouble on emulated networks; be explicit.
  --dns 1.1.1.1
)

args=(
  push -W .github/workflows/action-selftest.yml
  --container-architecture "$PLATFORM"
  -P ubuntu-latest=catthehacker/ubuntu:act-latest
  --container-daemon-socket -
  # Use the runner image already pulled. Re-pulling on every run is slow, and
  # on a machine with a stale credential helper in ~/.docker/config.json it
  # fails outright with "docker-credential-desktop: not found" -- a confusing
  # way to learn nothing is wrong with the workflow. Pull it once by hand:
  #   docker pull catthehacker/ubuntu:act-latest
  --pull=false
  --container-options "${opts[*]}"
)
[ -n "$JOB" ] && args+=(-j "$JOB")

echo "== act ${JOB:-(all jobs)} =="
exec act "${args[@]}"
