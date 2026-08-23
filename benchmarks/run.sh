#!/usr/bin/env bash
# Clone the working corpus and run veripp against the reference targets.
# Informational: prints outcomes, does not assert them (they shift with the
# pinned ESBMC version).
set -uo pipefail
cd "$(dirname "$0")/.."
WORK="${1:-$(mktemp -d)}"
echo "corpus dir: $WORK"

for repo in lvandeve/lodepng nothings/stb; do
  name=$(basename "$repo")
  [ -d "$WORK/$name" ] || git clone -q --depth 1 "https://github.com/$repo.git" "$WORK/$name"
done

run() {
  echo
  echo "== veripp verify $* =="
  uv run veripp verify "$@" --no-llm --timeout 90 2>&1 | sed -n '1,4p'
}

run "$WORK/lodepng/lodepng.cpp" --function lodepng_addofl
run "$WORK/lodepng/lodepng.cpp" --function reverseBits
run "$WORK/lodepng/lodepng.cpp" --function lodepng_strlen
run "$WORK/stb/stb_image_write.h" --function stbiw__zlib_bitrev -D STB_IMAGE_WRITE_IMPLEMENTATION
