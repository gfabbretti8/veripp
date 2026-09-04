# Heap buffer over-read in parson's `json_value_init_string_with_len`

**Status: not reported upstream.** Internal record.

| | |
|---|---|
| Project | [parson](https://github.com/kgabis/parson) — single-file C JSON library |
| Entry point | `json_value_init_string_with_len` — **public API** |
| Found by | `veripp`, harness generated from the signature, no manual harness |
| Confirmed by | AddressSanitizer |
| Class | CWE-125 out-of-bounds read |
| Trigger | 4 bytes, no crafted state, first call |

The API takes an explicit length so the caller may pass a buffer that is
**not** NUL-terminated. Doing exactly that reads past the end.

---

## The defect

Three functions, each individually reasonable:

```c
/* parson.c:350 — takes NO length. Decides width from the lead byte. */
static JSON_Status verify_utf8_sequence(const unsigned char *string, int *len) {
    *len = num_bytes_in_utf8_sequence(string[0]);
    if (*len == 1) { cp = string[0]; }
    else if (*len == 2 && IS_CONT(string[1])) { ... }              /* reads [1]    */
    else if (*len == 3 && IS_CONT(string[1]) && IS_CONT(string[2])) { ... }
    else if (*len == 4 && IS_CONT(string[1]) && IS_CONT(string[2])
                       && IS_CONT(string[3])) { ... }              /* reads [1..3] */
```

```c
/* parson.c:392 — checks only where the sequence STARTS. */
static int is_valid_utf8(const char *string, size_t string_len) {
    const char *string_end = string + string_len;
    while (string < string_end) {                  /* start is in bounds ... */
        if (verify_utf8_sequence(...) != JSONSuccess)   /* ... the sequence may not be */
            return PARSON_FALSE;
        string += len;
    }
```

```c
/* parson.c:1653 — public API, called on the caller's buffer. */
JSON_Value * json_value_init_string_with_len(const char *string, size_t length) {
    if (!is_valid_utf8(string, length)) {
        return NULL;
    }
```

`verify_utf8_sequence` has no way to know where the buffer ends — it is
given a bare pointer. `is_valid_utf8` has the length but never checks that
the *whole* sequence fits: it tests `string < string_end`, not
`string + len <= string_end`.

So a buffer whose **last byte is a UTF-8 lead byte** (`0xC2`–`0xF4`) makes
the validator read beyond it:

| last byte | claims | bytes read past the end |
|---|---|---|
| `0xC2`–`0xDF` | 2-byte sequence | 1 |
| `0xE0`–`0xEF` | 3-byte sequence | 1–2 |
| `0xF0`–`0xF4` | 4-byte sequence | 1–3 |

How far it actually runs depends on whether the bytes that happen to follow
in memory look like continuation bytes (`10xxxxxx`), because `&&`
short-circuits. At minimum one byte is always read.

## Why the API's own contract makes this reachable

This is not an exotic misuse. `json_value_init_string_with_len` exists
*because* the caller has a length rather than a C string — parson's other
entry point already covers the terminated case:

```c
/* parson.c:1651 */
return json_value_init_string_with_len(string, strlen(string));
```

A caller holding a length-delimited slice — a substring, a field of a
network buffer, a `mmap`ed region, a Rust/Go `&[u8]` crossing FFI — is
using the function exactly as documented, and is the caller that gets the
over-read. If the buffer happens to be NUL-terminated the read stops at the
terminator, which is why the terminated path never shows it.

## Reproduction

```c
const size_t length = 4;
char *buf = malloc(length);          /* exactly 4 bytes, no terminator */
buf[0] = 'a'; buf[1] = 'b'; buf[2] = 'c';
buf[3] = (char) 0xF0;                /* lead byte: claims 3 more follow */

json_value_init_string_with_len(buf, length);
```

```bash
cc -g -fsanitize=address -I parson -o repro repro_parson_utf8.c parson/parson.c
./repro
```

```
=================================================================
==74750==ERROR: AddressSanitizer: heap-buffer-overflow on address
0x6020000000d4 at pc 0x000104184844
READ of size 1 at 0x6020000000d4 thread T0
    #0 0x000104184840 in verify_utf8_sequence parson.c:363
    #1 0x000104177720 in is_valid_utf8 parson.c:396
    #2 0x000104177534 in json_value_init_string_with_len parson.c:1659
    #3 0x000104174a80 in main repro_parson_utf8.c:40

0x6020000000d4 is located 0 bytes after 4-byte region
[0x6020000000d0,0x6020000000d4) allocated by thread T0 here:
    #0 0x000104a5130c in malloc
    #1 0x0001041748b4 in main repro_parson_utf8.c:30
```

## Severity

Moderate-low, and worth being precise:

* It is a **read**, not a write — no corruption, no control-flow impact.
* Up to three bytes past the allocation. On a heap with padding it usually
  reads slack; against an unlucky page boundary it faults.
* The bytes read influence only the accept/reject decision, so at most it
  makes `json_value_init_string_with_len` return `NULL` (or not) based on
  adjacent memory. That is a correctness bug in its own right: the same
  input can validate differently depending on what sits next to it in
  memory.
* No crafted state, no prior calls, no special build. Four bytes on the
  first call.

## Suggested fix

Give the validator the remaining length and check the whole sequence fits:

```c
static int is_valid_utf8(const char *string, size_t string_len) {
    int len = 0;
    const char *string_end = string + string_len;
    while (string < string_end) {
        if (verify_utf8_sequence((const unsigned char *) string, &len) != JSONSuccess) {
            return PARSON_FALSE;
        }
        if (len <= 0 || (size_t) len > (size_t) (string_end - string)) {
            return PARSON_FALSE;      /* truncated sequence at the end */
        }
        string += len;
    }
    return PARSON_TRUE;
}
```

Checking after the call still reads out of bounds inside
`verify_utf8_sequence`, so the durable fix is to pass the remaining length
into it and have it refuse a sequence that does not fit.

## Why no fuzzer found it

parson's fuzzing entry points parse JSON *documents*, and a document is a
NUL-terminated buffer — the path where the terminator hides the read.
`json_value_init_string_with_len` is a construction API: it builds a JSON
value from a caller's bytes and is never exercised by parsing a document.
Reaching it needs a harness that calls it directly with a length-delimited,
unterminated buffer, which is exactly the harness `veripp` generated from
the signature and nobody had written by hand.
