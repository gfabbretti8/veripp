# Field report: pointing veripp at five real C libraries

**Status: internal record. Nothing here has been reported upstream.**

This is what happened when veripp was aimed at code it had never seen, with
a standing rule that no counterexample counted until it survived triage and
a sanitizer.

The headline is not the bugs. It is the ratio.

| | count |
|---|---:|
| Functions proved free of the eight UB classes | **159** |
| Real defects confirmed (sanitizer-verified) | **4** |
| Counterexamples that turned out to be false | **9** |
| veripp modelling defects that had to be fixed first | **12** |

Every one of the nine false positives was caught by reading the
assumptions veripp prints beside each result. Not one was caught by
intuition, and several looked *more* convincing than the real findings.

---

## What was scanned

| library | surface | proved | real bugs |
|---|---|---:|---:|
| lwIP | `def.c` string helpers | 1 | **3** |
| cJSON | whole file, 106 functions | 31 | 0 |
| nanopb | `pb_encode.c` | 11 | 0 |
| mbedTLS 3.6 | base64 | 5 | 0 |
| mbedTLS 3.6 | pem | 7 | 0 |
| mbedTLS 3.6 | asn1write (DER) | 16 | 0 |
| mbedTLS 3.6 | x509write_csr | 8 | 0 |
| mbedTLS 3.6 | x509write_crt | 18 | 0 |
| miniz | core | 14 | 0 |
| miniz | tdef / tinfl | 9 | 0 |
| miniz | zip | 34 | 0 |
| parson | whole file, 144 functions | 30 | **1** |
| tinyexpr | whole file | 18 | 0 |

Three real defects are in [LWIP_STRING_HELPERS.md](LWIP_STRING_HELPERS.md);
the fourth, an out-of-bounds read reachable from parson's **public** API
with four bytes, is in [PARSON_UTF8_OVERREAD.md](PARSON_UTF8_OVERREAD.md).
None is reported upstream.

## The nine false positives

Each of these was a counterexample that a less careful reader would have
filed as a bug.

1. **`cJSON_Minify`** — the harness modelled a mutable `char *` as a
   two-byte buffer that was not NUL-terminated, so the function "read past
   the end" of a string the caller never promised. *Cause: only
   `const char *` was treated as a C string.*
2. **`case_insensitive_strcmp`** — same, for `unsigned char *`, which is
   how cJSON and most byte-handling C spell every string.
3. **`lwip_strnstr` / `lwip_strnistr` (first pass)** — one length parameter
   was applied to *every* pointer, so `token` was bounded by `n` instead of
   its terminator and the over-read landed inside ESBMC's own `strlen`.
4. **`mbedtls_pem_write_buffer`** — looked like an out-of-bounds **write**
   until `mbedtls_base64_encode` was linked. With the real callee present
   it verifies. *Cause: an unlinked callee is modelled as side-effect-free,
   so the length arithmetic downstream was garbage.*
5. **`mbedtls_asn1_write_named_bitstring`** — counterexample was
   `bits = SIZE_MAX` against a small buffer. The caller's contract is that
   `buf` holds `(bits+7)/8` bytes; the harness cannot infer that relation.
6. **`oid_subidentifier_encode_into`** — a fabricated out-of-bounds write
   in mbedTLS's OID encoder, caused by veripp assuming DER's *backwards*
   cursor convention for a *forward* writer. `bound - *p` went negative,
   wrapped huge as `size_t`, and sailed past the guard. **The most
   dangerous of the nine**: plausible mechanism, plausible location,
   security-relevant file.
7. **`tdefl_record_match`** — miniz's own `MZ_ASSERT` failing, because the
   harness called an internal function without the precondition the
   assertion documents. Every real call site normalises the value first.

8. **`mz_zip_writer_create_zip64_extra_data`** — an out-of-bounds write
   into a buffer with no length parameter. It writes at most 28 bytes and
   every caller declares exactly `MZ_ZIP64_MAX_CENTRAL_EXTRA_FIELD_SIZE`
   (28); veripp cannot infer that from a body whose writes go through
   `MZ_WRITE_LE64` macros.
9. **`mz_write_le64`** — same cause, one level down.

Two further classes were caught the same way: nanopb's `pb_enc_*`
counterexamples (structs whose members sit inside `#ifdef` blocks, so the
harness NULLed pointers it could not type), and `mz_inflateReset` (a
nondeterministic struct with a NULL nested pointer).

## The twelve fixes this required

Ordered as encountered. Each was found because a counterexample failed
triage — none by a failing test.

1. Mutable `char *` walked to a terminator is a C string.
2. `unsigned char *` and `signed char *` likewise.
3. Delegation to `<string.h>` (`strlen(token)`) is terminator evidence.
4. A pointer with terminator evidence is not paired with a length.
5. The length scan stops at the next pointer, so
   `f(dst, dlen, olen, src, slen)` stops modelling the scalar out-parameter
   `olen` as an array.
6. Structs with preprocessor-conditional members are refused outright
   rather than modelled from merged branches.
7. The `(T **p, T *start)` backwards DER cursor is modelled — this took
   mbedTLS's `asn1write.c` from **5% harnessable to 91%**.
8. `const unsigned char *` is binary data, not a string.
9. …but a const char-like pointer in a function that walks *anything* to a
   NUL is still a string, which strcmp-shaped pairs need.
10. Cursor direction is read from the body, not assumed.
11. …falling back to the companion's name (`start` vs `bound`) when the
    body delegates, which otherwise cost 13 functions.
12. `doctor` warns that arm64 hosts cannot parse ARM intrinsics.

All twelve are regression-clean at 578 tests.

## Lessons that generalise

**An automatic verifier's raw output is not a bug list.** Nine of twelve
counterexamples on real code were the harness's fault, not the code's. A
tool that emits counterexamples without disclosing the assumptions behind
them is unusable for this work — you cannot tell the 9 from the 3.

**The disclosure is the product.** Every false positive here was
identified from the assumption block: "points to exactly `n` valid
elements", "these callees are declared but not defined", "field
combinations no real caller can produce". That machinery earns its keep.

**Refusing beats guessing, but refusal has a cost.** Making veripp refuse
unclear cursor directions was correct and immediately cost 13 of 20
functions in one file — invisible from the change itself. A refusal that
quietly erases coverage is its own failure mode; it needs a coverage
regression check, not just a test suite.

**Inconclusive results lie too.** miniz first reported 19/19 inconclusive,
which reads as "this code defeats analysis". It was `-j 4` at a 40-second
timeout starving each job. Re-run singly, the functions verify. This is the
mirror image of a false positive and harder to notice, because it hides a
surface instead of inventing a defect in it.

**A buffer with no length parameter cannot be verified, only guessed at.**
Three of the nine false positives reduce to this: `pBuf`, `buf`, `token` —
the size lives in the caller's head or in a macro constant, and no analysis
of the callee can recover it. This is the single largest source of noise on
real C.

**Obscure beats famous.** Every real defect came from small, widely
embedded, lightly audited code — lwIP's string helpers and parson's UTF-8
validator. The famous libraries went 153 proofs to zero bugs. Sixteen ticks
were spent on mbedTLS and miniz before switching targets on that evidence;
the switch produced a finding in one tick.

**Well-maintained C survives this.** mbedTLS produced 54 proofs and zero
defects; miniz produced 57 and zero. That is the expected and correct
result for audited code, and worth stating plainly rather than treating a
quiet scan as a failed hunt.

## Reproducing

Everything ran either natively or in veripp's own image under x86_64
(arm64 cannot parse ARM intrinsics; see the `doctor` note):

```bash
docker run --rm --platform linux/amd64 --entrypoint sh veripp-hunt-amd64 -c \
  '/opt/veripp/bin/veripp scan /targets/mbedtls36/library/asn1write.c \
     -I /targets/mbedtls36/include -I /targets/mbedtls36/library \
     --no-llm --timeout 30 -j 2'
```
