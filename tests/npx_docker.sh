#!/usr/bin/env bash
# Run the npx installer tests in Docker, across Node versions.
#
# Node on a developer machine is often whatever homebrew last did to it; these
# images are what users actually have. Also runs on Linux, which is where most
# of them are.
#
#   ./tests/npx_docker.sh              # node 18, 20, 22
#   ./tests/npx_docker.sh node:22      # one image
set -euo pipefail
cd "$(dirname "$0")/.."
images=("$@")
[ ${#images[@]} -eq 0 ] && images=(node:18-alpine node:20-alpine node:22-alpine)

# The test script must be somewhere the container runtime shares: colima and
# Lima share $HOME but not /tmp or /var/folders, and a bind mount from there
# silently becomes an empty file.
stage="${TMPDIR_SHARED:-$HOME/.cache/veripp-npx}"
mkdir -p "$stage"
cp npm/test-install.sh "$stage/t.sh"

status=0
for image in "${images[@]}"; do
  docker run --rm -v "$PWD:/w:ro" -v "$stage/t.sh:/t.sh:ro" "$image" sh -c '
    cp -r /w/npm /w/skills /w/package.json /w/README.md /w/LICENSE /tmp/
    cd /tmp && npm pack --silent >/dev/null 2>&1
    npm install -g /tmp/veripp-skill-*.tgz >/dev/null 2>&1
    sh /t.sh' || status=1
done
exit "$status"
