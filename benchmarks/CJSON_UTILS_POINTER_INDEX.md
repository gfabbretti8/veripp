# JSON Pointer array indices are misparsed in cJSON_Utils

**Status: not reported upstream.** Internal record.

| | |
|---|---|
| Project | [cJSON](https://github.com/DaveGamble/cJSON) v1.7.18, `cJSON_Utils.c` |
| Entry points | `cJSONUtils_GetPointer`, `cJSONUtils_ApplyPatches` — **public API** |
| Found by | `veripp`, harness generated from the signature, then read by hand |
| Class | CWE-20 improper input validation → parser differential |
| Trigger | one JSON Patch, no crafted state, first call |
| Memory safety | **not** affected — no overread, no corruption |

An array index in a JSON Pointer is digits, and nothing else. cJSON_Utils
accepts `/1a`, resolves it to element **59**, and lets a patch write there.

---

## The defect

One character, on line 285:

```c
static cJSON_bool decode_array_index_from_pointer(const unsigned char * const pointer,
                                                  size_t * const index)
{
    ...
    for (position = 0; (pointer[position] >= '0') && (pointer[0] <= '9'); position++)
    {                                                 /* ^^^^^^^^^^ */
        parsed_index = (10 * parsed_index) + (size_t)(pointer[position] - '0');
    }

    if ((pointer[position] != '\0') && (pointer[position] != '/'))
    {
        return 0;
    }
    *index = parsed_index;
    return 1;
}
```

The second test reads `pointer[0]` where it means `pointer[position]`. Once
the first character is a digit it is true for the rest of the loop, so the
only surviving condition is `pointer[position] >= '0'` — satisfied by every
byte from `0x30` up. Letters, `:;<=>?@`, `[\]^_`, `` ` ``, `{|}~`, DEL and
everything ≥ `0x80` are all consumed and accumulated as though they were
digits.

`'a' - '0'` is 49, so `"1a"` parses as `10 * 1 + 49 = 59`.

The loop still stops at any byte below `0x30` — `'\0'` and `'/'` among them —
so it stays inside the string. **This is not a memory-safety bug.** The
terminating check then passes, because the loop stopped exactly where that
check is satisfied, and the function reports success.

## RFC 6901

> The reference token is converted to an array index by interpreting it as a
> base-10 integer composed only of the characters `0`-`9`.

A token containing anything else is not an array index, and an
implementation must not resolve it as one.

## Reproduction

```c
cJSON *doc = cJSON_CreateArray();               /* 100 elements, "elem<i>" */
for (int i = 0; i < 100; i++) { ... }

cJSON *patch = cJSON_Parse(
    "[{\"op\":\"replace\",\"path\":\"/1a\",\"value\":\"OWNED\"}]");

int rc = cJSONUtils_ApplyPatches(doc, patch);
```

```
ApplyPatches(path="/1a") returned 0 (0 == applied)
  [57] = elem57
  [58] = elem58
  [59] = OWNED
  [60] = elem60
  [61] = elem61
```

Reads go the same way, through the same function:

```
  /1a      -> "elem59"
  /1b      -> "elem60"
  /1:      -> "elem20"
  /2~      -> "elem98"
```

## Severity

A parser differential, and worth being precise about what that is and is not.

* **Not** memory corruption. Nothing is read or written outside an
  allocation, no sanitizer fires, and the process does not misbehave.
* It **is** a write to an element other than the one the path names, through
  a public API, reached by one patch document.
* The risk is in the gap between two implementations. A service that
  validates a patch path before applying it — an allowlist of paths, a
  regex like `^/config/[0-9]+$`, or simply a second JSON Pointer library —
  will read `/1a` as invalid, or as the object key `1a`. cJSON_Utils applies
  it to array index 59. Validation and application then disagree about what
  the document says, which is the whole mechanism behind this bug class.
* The reachable indices are constrained, not arbitrary: two characters give
  `10*(d-'0') + (c-'0')`, so 10 through 168 with the first character a
  digit. A leading `0` is separately rejected. The accumulator is a `size_t`
  with no overflow check and wraps on a long enough token, but a token that
  wraps to a chosen small index has not been constructed here and is not
  claimed.
* Only reachable through `cJSON_Utils`, which is a separate translation unit
  most cJSON users do not compile. cJSON's core parser is unaffected.

## Suggested fix

```c
    for (position = 0; (pointer[position] >= '0') && (pointer[position] <= '9'); position++)
```

An overflow guard on `parsed_index` would be worth adding at the same time;
nothing currently stops a long run of digits from wrapping.

## Why no fuzzer found it

cJSON's fuzzing entry point parses JSON *documents* — `cJSON_Parse` on the
input buffer. `cJSON_Utils.c` is a different translation unit, is not built
into that harness, and is reached only by calling `cJSONUtils_*` directly
with both a document and a pointer or patch. Nothing in the fuzzing setup
constructs the second argument.

That is the same shape as the two confirmed findings before this one:
parson's over-read is in `json_value_init_string_with_len`, a construction
API its document fuzzers never call, and lwIP's three are in `def.c` string
helpers that its packet fuzzer never reaches. Three for three, the bug was
next door to heavily fuzzed code rather than in it.

## What veripp actually contributed

Honestly: it pointed at the function, and the counterexample it gave was
wrong.

veripp reported `decode_array_index_from_pointer` for an out-of-bounds read.
That report is a harness artifact — `pointer` was modelled as four
nondeterministic bytes with no terminator, because the body tests
`pointer[position] != '\0'` rather than `pointer[0]`, and veripp's
terminator evidence only recognises the literal `[0]` form. Given a real
NUL-terminated string the loop cannot run off the end.

So the finding came from reading the function that a false positive
nominated. That is worth recording as its own result: on this file the tool
raised five counterexamples out of 38 functions, and the value of the one
that was wrong was that it was *specific* — it named a twenty-line function
whose loop condition does not survive being read.
