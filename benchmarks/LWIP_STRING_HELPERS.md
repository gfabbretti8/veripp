# Three contract violations in lwIP's public string helpers

**Status: not reported upstream.** This is an internal record.

| | |
|---|---|
| Project | [lwIP](https://github.com/lwip-tcpip/lwip) (LWIP_VERSION 2.2.x) |
| Commit analysed | `d08f477` — 2026-09-01, `master` at time of writing |
| Found by | `veripp` (ESBMC), harnesses generated from signatures, no manual harness |
| Confirmed by | AddressSanitizer, standalone reproductions |
| Severity | Low — one-byte over-read; see [Severity](#severity) |

Both defects are the same mistake written twice: **the dereference is
evaluated before the length parameter is tested.** Each has a one-line fix.

The interesting part is not the size of the bug but *where it was hiding* —
in leaf functions no fuzzer reaches, because reaching them means a human
writing a harness for a string helper, which nobody does.

---

## Finding 1 — `lwip_strnstr` reads `buffer[n]`

**Location:** `src/core/def.c:105`, loop at **`def.c:112`**
(identical defect in `lwip_strnistr`, `def.c:128`, loop at **`def.c:135`**)

```c
char *
lwip_strnstr(const char *buffer, const char *token, size_t n)
{
  const char *p;
  size_t tokenlen = strlen(token);
  if (tokenlen == 0) {
    return LWIP_CONST_CAST(char *, buffer);
  }
  for (p = buffer; *p && (p + tokenlen <= buffer + n); p++) {   /* :112 */
    if ((*p == *token) && (strncmp(p, token, tokenlen) == 0)) {
      return LWIP_CONST_CAST(char *, p);
    }
  }
  return NULL;
}
```

C evaluates the left operand of `&&` first, so `*p` is read *before*
`p + tokenlen <= buffer + n` is tested.

Walk it with `tokenlen == 1` and a buffer of `n` bytes containing no NUL:

| iteration | `p` | `*p` | bound `p + 1 <= buffer + n` |
|---|---|---|---|
| … | `buffer + n - 2` | in bounds | true → continue |
| … | `buffer + n - 1` | in bounds (last valid byte) | `n <= n` **true** → continue |
| … | `buffer + n` | **out of bounds — read happens here** | false → exit, too late |

With a 2-byte token the bound fails one iteration earlier and nothing is
read past the end. **Only a single-character token reaches the defect.**

This matters more than Finding 2 because the function *takes an explicit
length*. A caller passes `n` precisely to say "this is not a C string,
only `n` bytes are mine." The function reads `n + 1`.

### Reproduction

`repro_strnstr.c`:

```c
#include <stdlib.h>
#include <string.h>
char *lwip_strnstr(const char *buffer, const char *token, size_t n);

int main(void) {
    const size_t n = 8;
    char *data = malloc(n);
    memset(data, 'A', n);            /* length-delimited, no terminator */
    lwip_strnstr(data, "Z", n);      /* single-character token */
    free(data);
    return 0;
}
```

```bash
cc -g -fsanitize=address -o repro repro_strnstr.c lwip_str.c && ./repro
```

```
=================================================================
==16301==ERROR: AddressSanitizer: heap-buffer-overflow on address
0x6020000000d8 at pc 0x0001009049bc bp 0x00016f4fa960 sp 0x00016f4fa958
READ of size 1 at 0x6020000000d8 thread T0
    #0 0x0001009049b8 in lwip_strnstr lwip_str.c:21
    #1 0x00010090488c in main repro_strnstr.c:34

0x6020000000d8 is located 0 bytes after 8-byte region
[0x6020000000d0,0x6020000000d8) allocated by thread T0 here:
    #0 0x00010116130c in malloc
    #1 0x000100904818 in main repro_strnstr.c:28
```

### Reachability — remote, via the HTTP server

`src/apps/http/httpd.c` parses the request directly out of the pbuf:

```c
data = (char *)p->payload;    /* raw received bytes */
data_len = p->len;            /* no NUL terminator anywhere */
...
crlf = lwip_strnstr(data, CRLF, data_len);           /* :2048 tokenlen 2 — safe */
...
left_len = (u16_t)(data_len - ((sp1 + 1) - data));
sp2 = lwip_strnstr(sp1 + 1, " ", left_len);          /* :2080 tokenlen 1 — REACHES IT */
```

Every `lwip_strnstr`/`lwip_strnistr` call in httpd, with token length:

| line | token | length | reaches defect |
|---|---|---|---|
| 1827 | `CRLF CRLF` | 4 | no |
| 1834 | `"Content-Length: "` | 16 | no |
| 1836 | `CRLF` | 2 | no |
| 2048 | `CRLF` | 2 | no |
| **2080** | **`" "`** | **1** | **yes** |
| 2084 | `CRLF` | 2 | no |
| 2097 | `CRLF CRLF` | 4 | no |
| 2436 | `CRLF CRLF` | 4 | no |

Trigger conditions, all attacker-controlled:

1. `data_len >= MIN_REQ_LEN` — send enough bytes.
2. A `CRLF` exists in the request (guard at `httpd.c:2048`).
3. **No second space** after the method, so the `" "` search never
   succeeds and runs to the end. This is the HTTP/0.9 shape that
   `LWIP_HTTPD_SUPPORT_V09` at `httpd.c:2082` explicitly supports.
4. No NUL byte in the remaining bytes — trivially true of a text request.

A request of the form `GET /x\r\n` satisfies all four.

---

## Finding 2 — `lwip_strnicmp` ignores `len` entirely when `len == 0`

**Location:** `src/core/def.c:186`, loop condition at **`def.c:210`**

```c
int
lwip_strnicmp(const char *str1, const char *str2, size_t len)
{
  char c1, c2;
  do {
    c1 = *str1++;              /* read precedes any test of len */
    c2 = *str2++;
    ...
    len--;                     /* len == 0  ->  underflows to SIZE_MAX */
  } while ((len != 0) && (c1 != 0));    /* :210 */
  return 0;
}
```

`strncmp(a, b, 0)` must compare zero characters and touch no memory. This
reads one byte from each operand first, then `len--` underflows `0` to
`SIZE_MAX`, after which the loop is bounded only by finding a NUL. On a
length-delimited buffer with none, the scan runs off the end.

### Reproduction

```c
char *a = malloc(4); memcpy(a, "AAAA", 4);   /* no terminator */
char *b = malloc(4); memcpy(b, "AAAA", 4);
lwip_strnicmp(a, b, 0);                      /* must touch nothing */
```

```
==72935==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x6020000000d4
READ of size 1 at 0x6020000000d4 thread T0
    #0 0x000102008d14 in lwip_strnicmp lwip_str.c:79
    #1 0x0001020088a0 in main repro_strnicmp.c:30
0x6020000000d4 is located 0 bytes after 4-byte region
```

### Reachability — public API, but the in-tree path is safe

`lwip_strnicmp` is public API (`src/include/lwip/def.h:137`). `len == 0`
is reachable:

```c
/* src/core/dns.c, dns_local_lookup() */
hostnamelen = strlen(hostname);
if (hostname[hostnamelen - 1] == '.') {
    hostnamelen--;                        /* :487   "."  ->  0 */
}
return dns_lookup_local(hostname, hostnamelen, addr ...);
    /* -> lwip_strnicmp(entry->name, hostname, 0)  at dns.c:503 / :516 */
```

`dns_local_lookup(".", &addr, type)` reaches `len == 0`. A second
decrement exists at `dns.c:1590`.

**This in-tree path is not memory-unsafe.** Both operands are
NUL-terminated C strings, so the runaway scan stops at a terminator that
is in bounds. The observable consequence is a *wrong answer* — "not
equal" where `strncmp` semantics require "equal" — not a read past an
allocation.

The other in-tree caller, `mdns_domain_eq` (`mdns_domain.c:323`), cannot
reach `len == 0`: its guard is `while (*ptra && ...)` with `len = *ptra`,
so `len >= 1` always.

Memory-unsafety therefore needs an out-of-tree caller passing `len == 0`
with an unterminated buffer — a reasonable thing to do, since the length
parameter is the API's promise that termination is not required.

---

## Finding 3 — `lwip_itoa` negates `INT_MIN`

**Location:** `src/core/def.c:221`, the negation on its third statement

```c
void
lwip_itoa(char *result, size_t bufsize, int number)
{
  char *res = result;
  char *tmp = result + bufsize - 1;
  int n = (number >= 0) ? number : -number;    /* -INT_MIN is undefined */
  ...
```

`-INT_MIN` is not representable as `int`, so the negation is undefined
behaviour (C17 6.5.3.3p3). veripp reports it directly:

```
Violated property: arithmetic overflow on neg
  CWE: CWE-190, CWE-191
  bufsize = 3
  number = -2147483648
```

Unlike the other two findings this one has a **visible functional
consequence**, not only a standards violation. `n` stays negative, so
`n % 10` yields negative remainders and `'0' + negative` produces
characters below `'0'`:

```
$ cc -g -fsanitize=undefined -o repro repro_itoa.c lwip_itoa.c && ./repro
lwip_itoa(buf, sizeof buf, INT_MIN)
lwip_itoa.c:12:36: runtime error: negation of -2147483648 cannot be
represented in type 'int'; cast to an unsigned type to negate this value
to itself
produced: "-./,),(-*,("   (expected "-2147483648")
```

The digits are punctuation because `'0' - 8` is `'('`, `'0' - 4` is `,`,
and so on.

### Reachability

`lwip_itoa` is public API (`src/include/lwip/def.h:133`) with four in-tree
callers:

| caller | argument | can it be INT_MIN? |
|---|---|---|
| `netif.c:1719` | `netif_index_to_num(idx)` | no — small positive |
| `mdns_domain.c:361` | a domain value | no |
| **`httpd.c:978`** | **HTTP Content-Length** | no — a file size |
| `makefsdata.c:1184` | content length (host tool) | no |

So it is latent in-tree: no caller can reach `INT_MIN`. It matters for
external callers, and `lwip_itoa` is exported precisely so applications can
use it.

A second, smaller issue sits one line above: `result + bufsize - 1` is
computed *before* the `bufsize < 2` guard, so `bufsize == 0` forms a
pointer before the start of the object — undefined behaviour in C even
though the pointer is never dereferenced.

### Suggested fix

Negate in unsigned, which is what the sanitizer message recommends:

```c
unsigned int n = (number >= 0) ? (unsigned int) number
                               : -(unsigned int) number;
```

and move `tmp` below the `bufsize < 2` guard.

## Also checked

`lwip_stricmp` (`def.c:151`) **verifies** — proved free of the checked
undefined-behaviour classes for two NUL-terminated strings within the
harness bound. It tests `c1` before advancing, which is exactly what the
other three fail to do.

---

## Severity

Low, and worth being precise rather than dramatic:

* The read is one byte past **`p->len`**, not necessarily past the
  allocation. lwIP pbuf payloads normally come from a pool whose buffer is
  larger than the bytes received, so in production this typically reads
  slack rather than faulting.
* The byte is used only in a comparison — never returned, echoed or
  branched on in a way that reveals it — so this is not an information
  leak.
* It is reliably caught by AddressSanitizer or Valgrind, and it is
  genuinely undefined behaviour.

A correctness and hygiene defect that happens to be remotely reachable —
not a remote crash. The reason to fix it is that the entire point of a
length-bounded API is that the length is honoured.

## Suggested fix

Test the bound before dereferencing:

```c
/* def.c:112 and def.c:135 */
for (p = buffer; (p + tokenlen <= buffer + n) && *p; p++) {

/* def.c:186 — check len before the first read */
while (len-- > 0) {
    c1 = *str1++;
    c2 = *str2++;
    ...
    if (c1 == 0) break;
}
```

## Why fuzzing was never going to find these

lwIP is fuzzed, and the harnesses feed **packets to the network stack** —
the right thing to fuzz, and the only thing anyone writes a harness for.
These are two-pointer-and-a-length leaf functions sitting below that;
reaching their interesting states through the stack means steering an
entire protocol parse.

A dedicated fuzz harness for a string helper is perfectly possible and
takes an engineer perhaps an hour per function. That hour is exactly why
it never happens, and it is the barrier `veripp` removes: both harnesses
here came from the function signatures alone.

The same structural gap, measured on cJSON: its OSS-Fuzz harness can
execute 21 of 106 functions. The other 85 are not under-fuzzed, they are
unreachable from the only entry point that has a harness.

## What it took to get a trustworthy answer

Four modelling defects in `veripp` had to be fixed first. Each was found
because a counterexample failed triage — not because a test caught it:

1. **Mutable `char *` was not modelled as a C string.** Only `const char *`
   was. `cJSON_Minify(char *json)` rewrites in place, cannot be const, and
   was handed a two-byte unterminated buffer.
2. **`unsigned char *` was not modelled as a C string.** cJSON — like most
   byte-handling C — spells every string that way, so *every* string
   function in it produced a fabricated out-of-bounds.
3. **Delegation to `<string.h>` was not terminator evidence.** A body
   calling `strlen(token)` has stated that `token` is terminated.
4. **One length parameter was applied to every pointer.**
   `lwip_strnstr(buffer, token, n)` bounds `buffer` by `n`; `token` is a
   string. Pairing both put the over-read inside ESBMC's own `strlen`
   rather than in the code under test — an artifact that looked exactly
   like a finding.

Before those fixes both lwIP functions produced counterexamples that were
**not real**. Twice, publishing early would have meant publishing
something false. The difference between artifact and defect was visible
only in the assumptions `veripp` prints with every result, which is the
argument for printing them.

## Novelty

No advisory, CVE or upstream report was found for either function. lwIP's
published buffer-overflow CVEs concern higher-level code —
`snmp_parse_inbound_frame` ([CVE-2026-8836](https://github.com/advisories/GHSA-3w8m-3w76-f6mh))
and `icmp6_send_response_with_addrs_and_netif`
([CVE-2020-22283](https://security.snyk.io/vuln/SNYK-DEBIAN11-LWIP-1534861)).
No published work applies ESBMC or CBMC to lwIP's string helpers.

Two caveats, stated rather than buried: absence from search results is not
proof of absence, and the history check used a shallow clone so it could
not search prior commits. What *is* established is that both defects are
present in `d08f477` (2026-09-01).

## Reproducing everything

```bash
git clone --depth 1 https://github.com/lwip-tcpip/lwip
# extract lwip_strnstr / lwip_strnistr / lwip_stricmp / lwip_strnicmp
# from src/core/def.c into lwip_str.c, add <string.h> and lwIP's
# LWIP_CONST_CAST macro, then:

veripp scan   lwip_str.c
veripp verify lwip_str.c --function lwip_strnstr --repro repro.c
cc -g -fsanitize=address -o repro repro.c lwip_str.c && ./repro
```

Expected: `lwip_stricmp` verifies; the other three report counterexamples;
the repro aborts under ASan with a heap-buffer-overflow read.
