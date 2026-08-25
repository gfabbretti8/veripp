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

## How many findings are real? (cJSON, triaged)

The table above counts counterexamples. A counterexample is not a bug: it
proves a property fails **in the generated harness**, which is not the same as
a caller being able to reach it. So the number a reader actually wants is how
many of those 33 survive triage. Here is that work, rather than an estimate.

### 14 of 33 were one artifact

Every `cJSON_Create*` finding was the same property — *"Incorrect alignment
when accessing data object"* — and each sits inside an `if (item)` guard, so
none is a null dereference. Reproduced in eight lines:

```c
static struct hooks global = { malloc };
struct S *p = (struct S *)global.allocate(sizeof(struct S));
if (p) { p->type = 1; }        /* "Incorrect alignment" */
```

The identical code calling `malloc` **directly verifies**. The checker cannot
establish the alignment of a pointer returned by a call it could not resolve,
so it assumes the worst — and cJSON allocates through `hooks->allocate`, as any
library with pluggable allocators does. None of these 14 say anything about
cJSON. veripp now classifies them as harness artifacts, with a note pointing at
`--link` or a compilation database as the way to check them properly.

### 5 more triaged in depth: none reachable

| finding | verdict | why |
|---|---|---|
| `cJSON_AddItemToObject` | harness artifact | fails inside `__memcpy_impl` with `malloc`/`free` stubbed; the CWEs are use-after-free and uninitialised-read, i.e. about the missing allocator |
| `cJSON_GetObjectItem` | precondition | fails in `case_insensitive_strcmp`, which walks until `*s == '\0'`. The harness builds objects whose `string` need not be NUL-terminated; every real one comes from `cJSON_strdup` and is |
| `cJSON_DetachItemFromObject` | precondition | calls `cJSON_GetObjectItem`; same root cause |
| `cJSON_Minify` | precondition | `while (json[0] != '\0')` with a two-byte harness buffer that need not be terminated; the documented contract is a C string |
| `cJSON_CreateTrue` | harness artifact | the alignment class above |

**Real bugs reachable from a public entry point: 0 of the 5 triaged, and 0 of
the 19 of 33 now accounted for.**

### How this was triaged

Twice, independently. Once by hand from the source, with the verdicts written
down before the second pass. Then by a Claude Haiku agent given only veripp's
own skill file and the findings, told to read the source and follow the call
chains. It agreed on all five, including the count, and noticed the `json[1]`
lookahead in `Minify` that the hand pass had missed. It took 168 seconds.

Two honest caveats. Agreement between two triagers is not proof — a shared
blind spot produces the same answer twice. It carries weight here only because
the alignment class has a standalone reproduction and the string cases are
plain in the source. And for `cJSON_CreateTrue` the agent partly repeated
veripp's own artifact note, so that verdict is less independent than the rest.

### What this means

On this library, the raw counterexample count overstates the findings a
maintainer would act on, by a lot. That is worth stating plainly rather than
quoting "33 counterexamples" and letting a reader assume 33 bugs. It is also
why the harness-artifact category exists, why `--link` and
`--compile-commands` matter more than they look, and why every counterexample
veripp prints carries the reminder that it holds in the generated harness.

The other 14 of 33 have not been triaged in depth. They are mostly the same
`Access to object out of bounds` in string comparison that the two above turned
out to be, but that is an expectation, not a measurement, and is recorded here
as one.

## The container reproduces these numbers

Every figure above was measured on the host. Scanning cJSON through the
published arm64 image gives **32 proved of 117 functions, 89% harnessable** —
the same proved count and the same harnessable rate. Counterexamples come out
higher (33 vs 19) because the table's runs had an LLM triaging findings, which
reclassifies some of them; the solver-side numbers are what match.

This comparison is worth repeating whenever the image changes. An earlier arm64
image passed the entire smoke suite while returning `parse_error` for 104 of
those 117 functions: it was missing clang's resource headers, so anything that
reached a system header failed, and nothing in a suite of self-contained
fixtures could see it. Aggregate numbers from a real library are what caught
it.

| [parson](https://github.com/kgabis/parson) | C JSON parser | 144 | 28 | 33 | 91% |
| [lz4](https://github.com/lz4/lz4) `lz4.c` | compression, everywhere | 94 | 19 | 18 | 57% |
| [tinyexpr](https://github.com/codeplea/tinyexpr) | expression evaluator | 47 | 17 | 5 | 96% |
| [zlib](https://github.com/madler/zlib) (6 modules) | the most deployed C library | 46 | 17 | 0 | 63% |
| [giflib](https://github.com/mirrorer/giflib) `dgif_lib.c` | GIF decoding | 23 | 2 | 12 | 78% |
| [jansson](https://github.com/akheron/jansson) `value.c` | C JSON, widely embedded | 88 | 20 | 31 | 90% |
| [libyaml](https://github.com/yaml/libyaml) `api.c` | YAML, behind PyYAML | 53 | — | — | 74% |

```bash
veripp scan path/to/libpng/png.c -I path/to/libpng --timeout 10 -j 4
```

jansson needs `-D HAVE_STDINT_H` and its generated `jansson_config.h`.
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

## Two ideas that measured worse

Objects are built with the library's own initialiser where one exists
(`T_init(T*)`). The obvious extension is the *factory* shape many C APIs use
instead — jansson's `json_object()`, returning a fresh `json_t*`. Implemented
and measured, it took jansson from 20 proved to 11 and from 31 leads to 40,
so it was removed.

Two reasons, both instructive. A factory is chosen by shape, and a type
usually has several (`json_object`, `json_array`, `json_null`) that produce
genuinely different objects — picking one narrows the question in a way the
caller never asked for. And calling a real constructor drags allocation into
every harness, which costs solver time that was buying proofs elsewhere.
An initialiser filling a caller-owned object has neither problem.

**Shrinking the bound when a run times out.** Timeouts are the largest
unhelpful outcome left (22 of jansson's 88 functions, 39 of cJSON's), and a
proof at unwind 2 is a weaker claim but a real one. Retrying a timed-out run
at a quarter of the bound changed jansson's inconclusive count from 28 to 28:
the timeouts became unwind-limit results instead, which is no more use. The
functions that time out are ones whose loops need a *larger* bound to say
anything, so making it smaller only reaches the bound sooner. Removed.

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
