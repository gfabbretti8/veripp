#!/usr/bin/env bash
# Rehearse .github/workflows/image.yml's release flow locally, against a
# throwaway registry. Nothing leaves this machine.
#
# The manifest job is the one piece of the release that cannot be tested by
# building an image: it only exists once two per-architecture digests have
# been pushed and stitched together. Getting it wrong produces a tag that
# silently serves one architecture to everybody, which is worse than a tag
# that does not exist. Run this after changing image.yml or the Dockerfile.
#
#   ./tests/manifest_rehearsal.sh
#
# Requires docker with buildx and QEMU/Rosetta for the non-native platform.
set -euo pipefail

PORT="${REGISTRY_PORT:-5001}"
REG="localhost:$PORT/veripp"
BUILDER="${BUILDER_NAME:-veripp-rehearsal}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cleanup_all() {
  docker rm -f veripp-rehearsal-registry >/dev/null 2>&1 || true
  docker buildx rm "$BUILDER" >/dev/null 2>&1 || true
}
[ "${KEEP:-0}" = "1" ] || trap 'rm -rf "$WORK"; cleanup_all' EXIT

echo "== throwaway registry on :$PORT =="
docker rm -f veripp-rehearsal-registry >/dev/null 2>&1 || true
docker run -d --name veripp-rehearsal-registry -p "$PORT:5000" registry:2 >/dev/null

# push-by-digest needs the docker-container driver -- the plain docker driver
# does not implement it. This is also what setup-buildx-action gives you in CI.
cat > "$WORK/buildkitd.toml" <<TOML
[registry."localhost:$PORT"]
  http = true
  insecure = true
TOML
docker buildx rm "$BUILDER" >/dev/null 2>&1 || true
docker buildx create --name "$BUILDER" --driver docker-container \
  --driver-opt network=host --config "$WORK/buildkitd.toml" --bootstrap >/dev/null

digests=()
for entry in "linux/amd64:amd64" "linux/arm64:arm64"; do
  platform="${entry%%:*}"; arch="${entry##*:}"
  echo "== $platform: build and push by digest =="
  docker buildx build --builder "$BUILDER" --platform "$platform" \
    --output "type=image,name=$REG,push-by-digest=true,name-canonical=true,push=true,registry.insecure=true" \
    --metadata-file "$WORK/meta-$arch.json" . > "$WORK/build-$arch.log" 2>&1 || {
      echo "   build failed; last lines:"; tail -20 "$WORK/build-$arch.log"; exit 1; }
  digest=$(python3 -c "import json;print(json.load(open('$WORK/meta-$arch.json'))['containerimage.digest'])")
  echo "   $digest"
  digests+=("$REG@$digest")
done

echo "== stitch the manifest =="
docker buildx imagetools create --builder "$BUILDER" \
  -t "$REG:rehearsal" "${digests[@]}" >/dev/null

echo "== both architectures must be in it =="
docker buildx imagetools inspect --builder "$BUILDER" "$REG:rehearsal" --raw \
  | python3 -c "
import json,sys
index = json.load(sys.stdin)
got = sorted({
    f\"{m['platform']['os']}/{m['platform']['architecture']}\"
    for m in index.get('manifests', [])
    if m.get('platform') and 'unknown' not in m['platform']['architecture']
})
print('   mediaType:', index.get('mediaType'))
print('   platforms:', got)
missing = [w for w in ('linux/amd64', 'linux/arm64') if w not in got]
if missing:
    sys.exit(f'   MISSING: {missing}')
print('   both architectures present')
"

echo "== the tag must resolve per host architecture =="
for platform in linux/amd64 linux/arm64; do
  docker rmi -f "$REG:rehearsal" >/dev/null 2>&1 || true
  docker pull -q --platform "$platform" "$REG:rehearsal" >/dev/null
  got=$(docker image inspect -f '{{.Architecture}}' "$REG:rehearsal")
  want="${platform##*/}"
  [ "$got" = "$want" ] || { echo "   $platform resolved to $got"; exit 1; }
  echo "   $platform -> $got"
done

echo "== and the image pulled through the manifest must still work =="
docker rmi -f "$REG:rehearsal" >/dev/null 2>&1 || true
docker pull -q "$REG:rehearsal" >/dev/null
native="$(docker image inspect -f '{{.Architecture}}' "$REG:rehearsal")"
SMOKE_TMPDIR="${SMOKE_TMPDIR:-$HOME/tmp}" PLATFORM="linux/$native" \
  "$(dirname "$0")/image_smoketest.sh" "$REG:rehearsal"

echo
echo "rehearsal passed: the release would publish a working multi-arch tag."
