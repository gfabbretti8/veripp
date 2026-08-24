# Benchmark corpus: real libraries veripp runs against

Found by probing popular single-TU libraries through the ESBMC 8.4 frontend
(`--goto-functions-only`) and then through the full veripp pipeline.
`./benchmarks/run.sh` reproduces everything below from a clean checkout.

## Working targets

| library | popularity | frontend | veripp coverage | notes |
|---|---|---|---|---|
| [lodepng](https://github.com/lvandeve/lodepng) | ~2k stars, ubiquitous PNG codec | OK | 54/260 functions (21%) | best target; exercises every triage category |
| [stb_image_write](https://github.com/nothings/stb) | ~30k stars (stb) | OK | 14/49 functions (29%) | needs `-D STB_IMAGE_WRITE_IMPLEMENTATION` |
| [cJSON](https://github.com/DaveGamble/cJSON) | ~12k stars | OK (as C++ TU) | 4/117 | most functions take `cJSON*` structs |
| [miniz](https://github.com/richgel999/miniz) | ~2k stars | OK (needs stub `miniz_export.h`) | 8/24 | project typedefs (`mz_ulong`) now resolve via local includes |
| [uthash](https://github.com/troydhanson/uthash) | ~4k stars | OK | n/a | macro library; no functions to target |

## Reference results (ESBMC master, defaults)

These are observations, not assertions. With Anthropic credentials set,
`./benchmarks/eval_triage.py` grades the live LLM triage against the
counterexample rows below (ground truth from the 2026-08-23 pilot):

| target | result | meaning |
|---|---|---|
| `lodepng.cpp --function lodepng_addofl` | **verified** | overflow-check helper proven (bounded) |
| `lodepng.cpp --function reverseBits` | counterexample: UB shift when `num > 32` | missing precondition — internal callers pass small `num` |
| `ring_buffer.cpp --class RingBuffer` | **verified** over all 4-call sequences | states built up across calls, not just the first |
| `lodepng.cpp --function lodepng_strlen` | **verified** | was a harness-artifact counterexample until `const char*` params got a NUL-terminated string model |
| `stb_image_write.h --function stbiw__zlib_bitrev` | counterexample: `shl` overflow | missing precondition on `codebits`; with LLM triage, becomes a solver-checked conditional proof |
| `miniz.c --function mz_adler32` | **verified** | the Adler-32 checksum core, via typedef resolution + `buf_len` pairing |

## Known-broken targets (upstream ESBMC defects, not veripp)

| library | failure | status |
|---|---|---|
| tinyxml2 | converter SIGSEGV, all platforms, v8.4 and master | unreported; 32-line reproducer in scratchpad spike |
| jsoncpp | frontend rejects (`basic_istringstream` over custom allocator) | [esbmc#7017](https://github.com/esbmc/esbmc/issues/7017) fixed on master post-v8.4; master then fails deeper in its `map` model |
| pugixml | frontend lacks `<iosfwd>` | unreported |
