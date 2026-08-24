# What veripp does on real libraries

Measured with `veripp scan`, ESBMC built from master, on an M1 Mac. Reproduce
any row with the command under it. These are observations at a point in time,
not a leaderboard — the numbers move with the solver timeout, and every one of
them is bounded by the assumptions each result states.

## Results

| library | what it is | functions | proved | leads | harnessable |
|---|---|---:|---:|---:|---:|
| [libpng](https://github.com/pnggroup/libpng) `png.c` | the PNG reference implementation | 70 | **40** | 12 | 86% |
| [lodepng](https://github.com/lvandeve/lodepng) | single-file PNG codec | 260 | **99** | 61 | 82% |
| [cJSON](https://github.com/DaveGamble/cJSON) | ubiquitous C JSON parser | 117 | 32 | 19 | 89% |
| [parson](https://github.com/kgabis/parson) | C JSON parser | 144 | 28 | 33 | 91% |
| [lz4](https://github.com/lz4/lz4) `lz4.c` | compression, everywhere | 94 | 19 | 18 | 57% |
| [tinyexpr](https://github.com/codeplea/tinyexpr) | expression evaluator | 47 | 17 | 5 | 96% |
| [zlib](https://github.com/madler/zlib) (6 modules) | the most deployed C library | 46 | 17 | 0 | 63% |
| [giflib](https://github.com/mirrorer/giflib) `dgif_lib.c` | GIF decoding | 23 | 2 | 12 | 78% |
| [libyaml](https://github.com/yaml/libyaml) `api.c` | YAML, behind PyYAML | 53 | — | — | 74% |

```bash
veripp scan path/to/libpng/png.c -I path/to/libpng --timeout 10 -j 4
```

libyaml needs its build run once first: it generates `config.h`, and veripp
says so rather than surfacing the compiler's complaint about an undefined
macro.

## Reading this honestly

**Proved** is the trustworthy column: no overflow, out-of-bounds read or
write, null dereference or division by zero, for **any** input within the
stated bounds and assumptions. That is a claim no fuzzer makes.

**Leads are not bugs.** Where an object has no initialiser for veripp to build
it from, the harness gives it every possible field value — including
combinations the type's own invariants forbid. 9 of libpng's 12 leads are of
that shape. Mechanically decidable artifacts are already filtered into a
separate count; what is left needs a human, or an LLM proposing preconditions
the solver then checks.

**Not harnessable is not failure.** lz4's 43% is mostly `void*`, which cannot
be constructed without knowing the intended type. Refusing is correct there;
guessing would produce a confident wrong answer.

## What these libraries taught the tool

Each row was also a bug report against veripp. Every one of these was
invisible while only lodepng was being measured, and each would have hit
someone pointing veripp at ordinary C:

| found in | veripp bug |
|---|---|
| tinyexpr | emitted a C++ harness for C, so `T *p = malloc(...)` would not compile |
| cJSON | macro-wrapped return types (`CJSON_PUBLIC(x) f(...)`) read as C++ syntax |
| cJSON | `static` locals initialised by a call — legal C++, invalid C |
| cJSON | `T * const p` not recognised as a pointer |
| parson | `typedef struct tag Alias;` not resolved to its tag |
| libyaml | `typedef struct tag { ... } Alias;` — the commonest C struct idiom |
| libyaml | angle-bracket includes of the project's own headers not followed |
| lz4 | `union` never recognised as a type at all |
| zlib | a `main()` behind `#ifdef` disqualified every function in the file |
| zlib | include chains deeper than two levels lost every typedef |
| zlib | `typedef Byte FAR Bytef;` — a macro expanding to nothing, mid-type |
| zlib | `typedef z_stream *z_streamp;` — pointer aliases |
| xxHash | a wrapper `.c` that only `#define`s and `#include`s crashed the scan |

The lesson is worth stating plainly: coverage measured on one codebase
measures that codebase. The bugs live in the idioms it happens not to use.
