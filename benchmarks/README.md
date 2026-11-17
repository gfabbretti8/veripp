# Benchmark corpus: real libraries veripp runs against

**See [CORPUS.md](CORPUS.md) for measured results across nine popular
libraries** — what was proved, what needs triage, and the veripp bugs each
library exposed.

Re-measured on 2026-11-16 with the pinned ESBMC 8.6 and veripp's default
scan, on a 4-core x86_64 container. `./benchmarks/run.sh` reproduces the
reference results below from a clean checkout, and
`./benchmarks/measure_corpus.py` the corpus table.

## Working targets

Harnessable is the share of a file's functions veripp can build a harness
for. lodepng and cJSON are measured in [CORPUS.md](CORPUS.md), and their
figures here are copied from it. stb_image_write and miniz are not in it;
theirs are from the same day and settings.

| library | popularity | frontend | harnessable | notes |
|---|---|---|---|---|
| [lodepng](https://github.com/lvandeve/lodepng) | ~2k stars, ubiquitous PNG codec | OK | 69% of 256 functions; 72 proved | best target; exercises every triage category; 95% and 105 with `--preprocess` |
| [stb_image_write](https://github.com/nothings/stb) | ~30k stars (stb) | OK | 46/62 functions (74%); 9 proved | needs `-D STB_IMAGE_WRITE_IMPLEMENTATION` |
| [cJSON](https://github.com/DaveGamble/cJSON) | ~12k stars | OK | 98% of 117 functions; 56 proved | an earlier run's counterexamples are triaged in CORPUS.md |
| [miniz](https://github.com/richgel999/miniz) | ~2k stars | OK (needs stub `miniz_export.h`) | 23/24 functions (96%); 13 proved | `miniz.c` only |
| [uthash](https://github.com/troydhanson/uthash) | ~4k stars | OK | n/a | macro library; no functions to target |

## Reference results (ESBMC 8.6, defaults, 2026-11-16)

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

## Known-broken targets

Three C++ libraries veripp proves nothing in, as of 2026-11-16 with ESBMC
8.6. This table used to put all three down to the checker. Two of
tinyxml2's three causes were veripp's.

| library | what happens | whose |
|---|---|---|
| tinyxml2 | every harness was rejected with `unknown type name 'XMLPrinter'`: veripp called into `namespace tinyxml2` without opening it | veripp; **fixed** 2026-11-16 |
| tinyxml2 | 57 of 91 harnesses were rejected: veripp wrote `T obj;` for the object a member function is called on, whatever `T` was. `XMLNode` is abstract, `XMLElement` and `XMLAttribute` have private destructors because only an `XMLDocument` makes one, and 14 of the functions are private or protected | veripp; **fixed** 2026-11-17: these are refused with the reason, and none reaches the frontend. Building the object through the library (`XMLDocument::NewElement`) was tried by hand and crashes the checker, so it would not have settled them |
| tinyxml2 | the other 34 run out of time or memory, and some crash the checker (`esbmc exited with status -11` on `CloseElement`) | ESBMC; not reported from here |
| jsoncpp `json_value.cpp` | the frontend rejects the file: `no member named 'pop_back' in 'std::basic_string<char>'` | ESBMC's C++ library model |
| pugixml | the frontend rejects the header: `pugixml.hpp:383: constructor cannot be redeclared` | not investigated |
