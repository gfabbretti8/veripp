# Field report: pointing veripp at five real C libraries

**Status: internal record. Nothing here has been reported upstream.**

This is what happened when veripp was aimed at code it had never seen, with
a standing rule that no counterexample counted until it survived triage and
a sanitizer.

The headline is not the bugs. It is the ratio.

| | count |
|---|---:|
| Functions proved free of the eight UB classes | **163** |
| Real defects confirmed | **8** |
| Counterexamples that turned out to be false | **32** |
| veripp modelling defects that had to be fixed first | **26** |

Every one of the thirty-two false positives was caught by reading the
assumptions veripp prints beside each result. Not one was caught by
intuition, and several looked *more* convincing than the real findings.

---

## What was scanned

| library | surface | proved | real bugs |
|---|---|---:|---:|
| lwIP | `def.c` string helpers | 1 | **3** |
| cJSON | whole file, 116 functions | 35 | 0 |
| cJSON_Utils | whole file, 38 functions | 6 | **2** |
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
| lwIP | ip4_frag (IP reassembly) | 7 | 0 |
| lwIP | pbuf | 11 | 0 |
| lwIP | dns | 10 | 0 |
| lwIP | PPP: lcp | 22 | 0 |
| lwIP | PPP: ccp | 20 | 0 |
| lwIP | PPP: ipcp | 17 | 0 |
| lwIP | PPP: upap | 9 | 0 |
| lwIP | PPP: chap-new | 8 | 0 |
| lwIP | PPP: eap | 16 | 0 |
| lwIP | PPP: chap_ms (MS-CHAP) | 11 | 0 |
| lwIP | PPP: vj, pppos | 4 | 0 |
| lwIP | PPP: mppe (MPPE) | 1 | 0 |
| lwIP | netbiosns (NetBIOS responder) | — | **1** |
| lwIP | smtp (client, auth setup) | — | **1** |
| lwIP | SNMP BER/ASN.1 decoder | 10 | 0 |

Three real defects are in [LWIP_STRING_HELPERS.md](LWIP_STRING_HELPERS.md);
the fourth, an out-of-bounds read reachable from parson's **public** API
with four bytes, is in [PARSON_UTF8_OVERREAD.md](PARSON_UTF8_OVERREAD.md);
the seventh is the most serious of them: an unbounded, remotely triggerable
out-of-bounds read in lwIP's NetBIOS responder, from a single 50-byte UDP
datagram, in [LWIP_NETBIOS_NAME_DECODE.md](LWIP_NETBIOS_NAME_DECODE.md); the
eighth is the report's only out-of-bounds **write**, a hardcoded 64 against a
configurable buffer in lwIP's SMTP client, in
[LWIP_SMTP_AUTH_MEMSET.md](LWIP_SMTP_AUTH_MEMSET.md) -- not remote, not
default, and diagnosed by the compiler, all of which is said there.
The fifth and sixth are both in cJSON_Utils' JSON Pointer handling -- a
malformed array index that resolves to the wrong element, and escape
decoding that makes `add` create a key under the wrong name -- and are in
[CJSON_UTILS_JSON_POINTER.md](CJSON_UTILS_JSON_POINTER.md). Those two are
**not** memory-safety bugs: AddressSanitizer and UndefinedBehaviorSanitizer
are clean on both reproductions. None is reported upstream.

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

10. **`parson_strdup`, `parson_strndup`, `json_value_init_string`** — one
    cause, three reports. parson lets callers swap its allocator, so it
    calls through `static JSON_Malloc_Function parson_malloc = malloc`, and
    an indirect call to an intrinsic leaves the checker with no model of it.
    Every pointer downstream was unconstrained, and the first write to one
    was reported as CWE-416 use-after-free.
11. **`find_builtin`** (tinyexpr) — `strncmp(name, functions[i].name, len)`
    with a NUL planted inside `name`. strncmp stops there and reports a
    match, so the index that follows ran off the end of the string literal
    `"abs"`. No tokeniser can produce it; identifiers do not contain NULs.
12. **`is_decimal`** (parson) — the same shape.
13. **`npr`** (tinyexpr) — `NaN on ieee_mul`, because `ncr` returns `NAN`
    for `n < r` and tinyexpr uses NaN as its error value. Producing NaN is
    defined IEEE behaviour, not undefined behaviour. Left unfixed on
    purpose: NaN cannot come from two finite operands, so a NaN report
    genuinely means something happened upstream, and a rule to suppress
    these would hide the cases where that something is a bug.
14. **`json_object_init`** (parson) — `capacity * sizeof(size_t)` overflows
    at `capacity = 2^62`, `malloc` gets a small number, and the loop that
    follows writes `capacity` entries into it. A textbook allocation-size
    overflow, and unreachable: the only call site passes
    `MAX(cell_capacity * 2, ...)`, so reaching it needs a previous
    allocation of 2^64 bytes to have succeeded. The function is static.
    *This one only appeared after the allocator was resolved* -- before
    that, the run failed on the unconstrained pointer long before it got
    here.
15. **`json_serialize_string`** (parson) — an output buffer with no length
    parameter, the largest noise class on real C and the third instance of
    it in this report.
16. **`cJSON_DetachItemFromArray`** — `item->prev->next` on an `item` whose
    `prev` is null while it is not the list head. cJSON's sibling lists are
    circular in `prev`, so no list the API can build looks like that; the
    harness built one because it fills the graph field by field.
17. **`parse_string`** (cJSON) — a `parse_buffer` with `offset == length`,
    so `buffer_at_offset(b)[0]` is one past the end. The invariant the
    harness states, `offset <= length`, is the right one for the type;
    parse_string additionally needs `offset < length`, and gets it from
    `can_access_at_index(input_buffer, 0)` three lines up in its only
    caller. A fact about a call site, which is triage's job.
18. **`update_offset`** (cJSON) — `strlen` on a `printbuffer` whose
    `length` is its capacity, not its content. Its callers have just
    written a terminated chunk there. The counted-buffer model is right and
    cannot know that.
19. **`print_number`** (cJSON) — `NaN on ieee_sub`, because `sscanf` is not
    defined in the translation unit, so its write through `&test` is not
    modelled and `test` stays unconstrained. The same cause as
    `mbedtls_pem_write_buffer` above, four hundred lines of C apart.

The remaining thirteen fall into four classes already named above, and are
listed together because the class is the interesting part, not the function:

* **An internal function called without the precondition its call sites
  establish** — `mppe_rekey` (lwIP MPPE; `keylen` is a free `u8_t` in the
  harness, and `mppe_init` sets it to exactly 16 or 8 or returns without
  calling it), `snmp_asn1_enc_s32t` (`octets_needed = 65535` shifts by
  524280, and every caller passes the 1–4 that `snmp_asn1_enc_s32t_cnt`
  computes), `dns_call_found` and `dns_correct_response` (indexed by their
  callers' loop bounds). With miniz's `tdefl_record_match` and mbedTLS's
  `asn1_write_named_bitstring`, six instances. This is the largest class
  after output buffers, and nothing in a signature can prevent it.
* **A struct graph the library cannot build** — `detach_item_from_array`
  and `insert_item_in_array` (cJSON_Utils) join `cJSON_DetachItemViaPointer`:
  all three walk `->prev`, which is never null in cJSON's circular sibling
  lists and freely null in a graph filled field by field. This is the class
  `--sequence` exists to remove, and cJSON is out of solver budget for it.
* **An output buffer with no length parameter** — `encode_string_as_pointer`
  (cJSON_Utils; the caller sizes it with `pointer_encoded_length`),
  `lcp_addci` and `ipcp_addci` (sized by the matching `*_cilen`),
  `pppos_output_append` (assumes `PBUF_POOL_BUFSIZE`).
* **Something outside the translation unit** — `lcp_extcode` and
  `lcp_rprotrej` (the `protocols[]` definition lives in ppp.c),
  `chap_input` and `chap_respond` (unlinked `pbuf_alloc`).

And one that is none of those: `eap_state_name` indexes a table with an
enum, and veripp gives an enum any representable value rather than only its
declared enumerators. That is deliberate -- a caller can pass anything
through an integer conversion -- but on a debug-only function whose callers
pass a live state, it is noise.

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
13. A pointer from an allocator with no body is an artifact, not a finding
    -- but only when an allocator really is among the run's bodiless
    functions, since the same properties fire on genuine use-after-free.
14. The library's allocator hooks are pointed at wrappers that call the same
    allocators directly, so the checker can see through the indirection at
    all. This is the fix; 13 is the label for the cases it cannot reach.
15. A resolved hook is no longer *also* reported as an unresolved callee.
16. A buffer the body hands to a bounded `<string.h>` routine is text of the
    given length, with no terminator inside it.
17. A signed length parameter is non-negative -- left free it was negative
    half the time, and every str- and mem- routine turns that into a huge
    `size_t`.
18. Objects can be built by the library's own constructors, all of them
    offered at once so the solver picks (`--constructors`).
19. ...including the handle types no constructor returns, reached by
    constructing their owner and asking it.
20. A constructor-built object is freed with the library's own deallocator,
    so the leak in the harness does not answer instead of the question.
21. A run whose only failure was an artifact is asked again with
    `--multi-property`, because ESBMC stops at the first violation and an
    artifact therefore ends the run before anything else is checked.
22. `veripp verify FILE` with no `--function` has no generated harness, so
    the rule "a failure inside the harness is about the harness" no longer
    applies to every finding in the user's own file.
23. An allocator table that is a struct of function pointers is resolved
    like a scalar hook, and a parameter of that type is initialised from
    the library's own table rather than filled at random.
24. ...including one reached through a struct FIELD, which is where cJSON
    keeps it: inside `parse_buffer`.
25. A cursor struct's offset is inside the buffer it indexes.
26. A `char *` struct FIELD is a C string, the rule veripp already applied
    to parameters.

All twenty-six are regression-clean at 633 tests.

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

**A file that is compiled out reports as a file the tool cannot handle.**
lwIP's mppe.c came back 0 of 7 harnessable, every function refused for
taking a `ppp_pcb *` -- a type veripp had been constructing happily in five
other PPP files. The cause was not veripp. `MPPE_SUPPORT` was missing from
the hunt's lwipopts.h, so the whole file was inside a dead `#if` and the
preprocessed source contained neither `ppp_pcb` nor `ppp_mppe_state`. With
one line added to the configuration it is 7 of 7.

This is the same failure mode as the miniz budget starvation and the
function-like macros in vj.c, and it is now the third instance: an output
that understates coverage reads as "the tool tried and this code defeated
it", and the surface gets skipped. Nothing distinguishes it from a genuine
refusal without going and looking. A `--preprocess` run that finds none of
the file's own types is a signal worth reporting on its own.

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

**The noise was not evenly spread; it was four causes.** cJSON was rescanned
after each fix, and its counterexample count is the clearest record of what
those causes were worth:

| after | counterexamples | proved |
|---|---:|---:|
| the original scan | 33 | 31 |
| resolving the allocator | 14 | 29 |
| looking past artifacts with `--multi-property` | 14 | 29 |
| bounding the cursor by its buffer | 11 | 29 |
| treating a `char *` field as a string | **4** | **35** |

All four survivors are false positives too, each for a different and
already-known reason. Not one of the twenty-nine that went away was a
missed bug -- they were the harness describing a cJSON that cannot exist:
an allocator that returns nowhere, a parse cursor past the end of its own
buffer, a `string` field one byte long. The proofs went *up* by four while
the noise fell by twenty-nine, which is the shape to expect when the fix is
to the model rather than to a threshold.

**Go where the fuzzer is not.** This is now the same lesson four times, and
it has produced every confirmed finding in this report. parson's
confirmed over-read is in `json_value_init_string_with_len`, a construction
API its fuzzers never call because they parse documents. lwIP ships its own
fuzzer in `test/fuzz`, which drives an ethernet frame through
netif -> ip4 -> udp/tcp/dns -- and the three confirmed lwIP defects are in
`def.c`, string helpers that path never touches. Scanning ip4_frag, pbuf and
dns afterwards produced 28 proofs and nothing else, which is the expected
result for code that is already fuzzed continuously.

cJSON is the cleanest case of the three, because both halves were measured.
Its core parser, which its fuzzer drives continuously, came back 35 proofs
and zero defects. `cJSON_Utils.c` is in the same repository, ships in the
same release, and is a separate translation unit the fuzzer does not build
-- and the first read of the first function a counterexample nominated found
a one-character bug that has been there for years.

`PPP_SUPPORT` does not appear in lwIP's fuzzing configuration at all. That
is 25 files of attacker-facing framing -- LCP, IPCP, CCP, EAP, PAP, CHAP,
MS-CHAP, VJ decompression, HDLC -- and it is the oldest code in the tree.
Reaching it took seven veripp fixes, because every function in it takes a
`ppp_pcb *` and ppp_pcb is a struct built out of `#if` blocks.

**Two invariants worth writing down, because veripp could not prove either
and both are correct.** lwIP's VJ decompression initialises
`comp->last_recv = 255` into a 16-entry `rstate[]`, and the implicit-index
path indexes it with no bound check. It is safe because `vj_compress_init`
also sets `VJF_TOSS`, and `VJF_TOSS` is cleared only on the two lines that
immediately follow a bounds-checked assignment to `last_recv`. Three
functions and one flag hold that together. veripp times out rather than
proving it -- a pbuf chain plus sixteen saved header states is too much
state -- which is the honest outcome and not a doubt about the code.

`snmp_asn1_dec_oid` maintains `oid_ptr == oid + *oid_len` across two
branches and a nested loop, guarded by `*oid_len < oid_max_len`, with the
first two writes covered by an `oid_max_len < 2` early return. That one
veripp does prove.

**Read the fuzzer's configuration before choosing a target, not after.**
Four of the findings in this report were noticed to be outside the project's
fuzzing after the fact. The fifth was found by inverting that: lwIP's
`test/fuzz/lwipopts.h` enables `LWIP_MDNS_RESPONDER` and `LWIP_SNMP`, so
mdns and snmp were ruled out despite both being name parsers of exactly the
shape that had been productive elsewhere. What it does not enable is
netbiosns, mqtt, tftp, smtp, sntp, http_client or PPP. netbiosns was picked
from that list because it is a listener that decodes a fixed-width name out
of an inbound datagram, and the first function read in it takes the length
of its output buffer and discards it with `LWIP_UNUSED_ARG`.

That is twenty minutes of reading a configuration file, and it beat every
heuristic about famous versus obscure libraries used earlier in this hunt.

**"No caller reaches this" is a claim, and it needs checking like any
other.** This report opened by saying lwIP's string helpers are leaf
functions no fuzzer reaches. True of the fuzzers, and false of lwIP:
`lwip_strnstr` over-reads only with a one-character token, eleven of its
twelve call sites pass CRLF or a header name, and the twelfth is the HTTP
request-line parser looking for the space before `HTTP/1.1`, on bytes off
the socket. Any request line without a second space runs the scan to the
bound.

It does not follow that this is a remote heap overflow, and the difference
took longer to establish than the reachability did. `data` is either a
static buffer declared with exactly one byte of slack, or a pbuf payload
inside a contiguous pool -- so in stock lwIP the byte read is adjacent or
stale rather than outside an allocation. The safety is circumstantial: it
rests on a `+ 1` in one declaration and on pool layout in the other, and a
driver handing up a tightly sized `PBUF_RAM` gets the real thing. Recording
both halves is the point. The first half alone overstates it and the second
half alone would have buried a reachable call site.

**Obscure beats famous.** Every real defect came from small, widely
embedded, lightly audited code — lwIP's string helpers and parson's UTF-8
validator. The famous libraries went 153 proofs to zero bugs. Sixteen ticks
were spent on mbedTLS and miniz before switching targets on that evidence;
the switch produced a finding in one tick.

**An unresolved allocator poisons everything downstream of it, silently.**
This was the single largest noise source found, and it hides as well as it
shouts. Loudly: three parson functions reported as use-after-free, and 14 of
cJSON's 33 counterexamples, all of them about `malloc` rather than the
library. Quietly: with every allocated pointer unconstrained, runs fail at
the first pointer they touch, so nothing *past* that point is ever checked
-- `json_object_init`'s allocation-size overflow only became visible once
the allocator was resolved. A tool that cannot see through a library's
allocator is not checking that library's allocating code at all, and its
output gives no hint of it.

**Constructing an object is a different question from filling its fields,
not a better one.** Field-filling asks about every field combination,
including ones the type forbids; a constructor asks only about objects the
library can build, and says nothing about states reached by mutating them.
Turning constructors on took four parson functions from counterexample to
proof -- `json_value_free` among them, which walks and frees the whole
object graph and was unreachable while every object was random fields with
a null pointer at depth two -- and took two others from counterexample to
timeout, because a real allocation and an accessor now run in front of every
call. That is why it is a flag and not the default.

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
