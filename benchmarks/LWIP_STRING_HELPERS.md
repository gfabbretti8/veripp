# Two length-parameter violations in lwIP's string helpers

Found with `veripp`, confirmed with AddressSanitizer, in lwIP master
`d08f477` (2026-09-01).

Both defects are the same mistake written twice: **the dereference is
evaluated before the length is checked.** Each is a one-line fix.

Neither is a crash-grade vulnerability, and this document says so plainly
in [Severity](#severity). What is interesting is not the size of the bug
but *where it was hiding*: in code that no fuzzer can reach, because
reaching it requires a human to sit down and write a harness for a string
helper, and nobody ever does.

---

## 1. `lwip_strnicmp` ignores its length entirely when `len == 0`

`src/core/def.c`:

```c
int
lwip_strnicmp(const char *str1, const char *str2, size_t len)
{
  char c1, c2;
  do {
    c1 = *str1++;          /* read happens before len is consulted */
    c2 = *str2++;
    ...
    len--;                 /* len == 0  ->  underflows to SIZE_MAX */
  } while ((len != 0) && (c1 != 0));
  return 0;
}
```

`strncmp(a, b, 0)` must compare zero characters and touch no memory. This
reads one byte from each operand first, then `len--` underflows `0` to
`SIZE_MAX`, after which `(len != 0)` is true for practical purposes and the
loop is bounded only by finding a NUL. On a length-delimited buffer that
has none, the scan runs off the end.

### Reproduction

```c
char *a = malloc(4); memcpy(a, "AAAA", 4);   /* no terminator */
char *b = malloc(4); memcpy(b, "AAAA", 4);
lwip_strnicmp(a, b, 0);                      /* must touch nothing */
```

```
ERROR: AddressSanitizer: heap-buffer-overflow
READ of size 1 at 0x6020000000d4
    #0 lwip_strnicmp def.c
0x6020000000d4 is located 0 bytes after 4-byte region
```

### Reachability

`lwip_strnicmp` is public API (`src/include/lwip/def.h`). `len == 0` is
reachable from public API:

```c
/* dns.c, dns_local_lookup() */
hostnamelen = strlen(hostname);
if (hostname[hostnamelen - 1] == '.') {
    hostnamelen--;                     /* "."  ->  hostnamelen == 0 */
}
return dns_lookup_local(hostname, hostnamelen, addr ...);
    /* -> lwip_strnicmp(entry->name, hostname, 0) */
```

So `dns_local_lookup(".", &addr, type)` reaches `len == 0`.

**But this in-tree path is not memory-unsafe.** Both operands there are
NUL-terminated C strings, so the runaway scan stops at a terminator that is
within bounds. The observable consequence is a wrong answer — the function
reports "not equal" where `strncmp` semantics require "equal" — not a
read past an allocation.

The other in-tree caller, `mdns_domain_eq`, cannot reach `len == 0` at
all: its loop guard is `while (*ptra && ...)` and `len = *ptra`, so `len`
is always ≥ 1.

The memory-safety failure therefore requires an out-of-tree caller passing
`len == 0` with a buffer that is not NUL-terminated — which is a
*reasonable* thing for a caller to do, because the length parameter is the
API's promise that termination is not required.

---

## 2. `lwip_strnstr` reads `buffer[n]`, one byte past the stated length

`src/core/def.c`:

```c
char *
lwip_strnstr(const char *buffer, const char *token, size_t n)
{
  const char *p;
  size_t tokenlen = strlen(token);
  if (tokenlen == 0) {
    return LWIP_CONST_CAST(char *, buffer);
  }
  for (p = buffer; *p && (p + tokenlen <= buffer + n); p++) {
```

C evaluates `*p` before the `&&`, so the bound is tested only *after* the
dereference. With `tokenlen == 1`, `p` advances to `buffer + n` and is
dereferenced there — one byte beyond what the caller declared valid.

This one matters more than the first, because the function *takes an
explicit length*. A caller passes `n` precisely to say "this is not a C
string; only `n` bytes are mine." The function then reads `n + 1`.

`lwip_strnistr` has the identical loop and the identical defect.

### Reproduction

```c
const size_t n = 8;
char *data = malloc(n);
memset(data, 'A', n);              /* length-delimited, no NUL */
lwip_strnstr(data, "Z", n);        /* single-char token */
```

```
ERROR: AddressSanitizer: heap-buffer-overflow
READ of size 1 at 0x6020000000d8
    #0 lwip_strnstr def.c
0x6020000000d8 is located 0 bytes after 8-byte region
```

### Reachability: remote, through the HTTP server

`src/apps/http/httpd.c` parses the request straight out of the pbuf:

```c
data = (char *)p->payload;      /* raw network bytes */
data_len = p->len;              /* no NUL terminator anywhere */
...
crlf = lwip_strnstr(data, CRLF, data_len);        /* tokenlen 2 - safe */
...
left_len = (u16_t)(data_len - ((sp1 + 1) - data));
sp2 = lwip_strnstr(sp1 + 1, " ", left_len);       /* tokenlen 1 - REACHES IT */
```

Only a **single-character token** can drive `p` to `buffer + n`; with
`CRLF` (2 bytes) the bound test fails first. Line 2080 is the one call in
httpd with a one-character token.

A request of the form `GET /x\r\n` — no second space, which is the
HTTP/0.9 shape the surrounding code explicitly supports — leaves no space
to find, so the scan walks to the end of the payload and reads one byte
past `p->len`.

---

## Severity

Low, and worth being precise about:

* The read is one byte past **`p->len`**, not necessarily past the
  allocation. lwIP pbuf payloads usually come from a pool whose buffer is
  larger than the bytes received, so in production this typically reads
  slack rather than faulting.
* The value read is used only in a comparison; it is not returned or
  echoed, so this is not an information leak.
* It is reliably detectable under AddressSanitizer or Valgrind, and it is
  genuinely undefined behaviour.

Call it a correctness and hygiene defect that is remotely reachable, not a
remote crash. The reason to fix it is that the whole point of a
length-bounded API is that the length is honoured.

## Suggested fix

One line each — test the bound before dereferencing:

```c
/* lwip_strnstr / lwip_strnistr */
for (p = buffer; (p + tokenlen <= buffer + n) && *p; p++) {

/* lwip_strnicmp: check len before the first read, e.g. */
while (len-- > 0) {
    c1 = *str1++;
    ...
}
```

---

## Why a fuzzer was never going to find these

lwIP is fuzzed. The harnesses feed **packets to the network stack**, which
is the right thing to fuzz and the only thing anyone writes a harness for.
`lwip_strnstr` sits below that: it is a two-pointer-and-a-length leaf
function, and reaching its interesting states through the TCP/IP stack
means steering an entire protocol parse.

Writing a dedicated fuzz harness for a string helper is possible, and takes
an engineer maybe an hour per function. That hour is exactly why it never
happens — and it is the barrier `veripp` removes: both harnesses here were
generated from the function signatures alone, with no human input beyond
naming the file.

This is the same structural gap measured on cJSON, where the project's
OSS-Fuzz harness can execute 21 of 106 functions; the other 85 are not
under-fuzzed but unreachable from the only entry point that has a harness.

## What it took to get a trustworthy answer

Four modelling defects in `veripp` had to be fixed first, each found because
the tool produced a counterexample that did not survive triage:

1. **Mutable `char *` was not modelled as a C string.** Only `const char *`
   was. `cJSON_Minify(char *json)` rewrites in place, so it cannot be
   const, and was handed a 2-byte unterminated buffer.
2. **`unsigned char *` was not modelled as a C string.** cJSON — like most
   byte-handling C — spells every string that way, so every string function
   in it produced a fabricated out-of-bounds.
3. **Delegation to `<string.h>` was not terminator evidence.** A body that
   calls `strlen(token)` has stated that `token` is terminated.
4. **One length parameter was applied to every pointer in the signature.**
   `lwip_strnstr(buffer, token, n)` bounds `buffer` by `n`; `token` is a
   string. Pairing both produced an over-read inside ESBMC's own `strlen`
   rather than in the code under test — an artifact that looked exactly
   like a finding.

Before those fixes, both lwIP functions produced counterexamples that were
**not** real. They are recorded here because the difference between the
artifact and the defect was invisible without reading the assumptions
`veripp` prints with every result — which is the argument for printing
them.

## Novelty

No advisory, CVE or upstream report for either function was found. lwIP's
published buffer-overflow CVEs concern higher-level code
(`snmp_parse_inbound_frame`, `icmp6_send_response_with_addrs_and_netif`).
No published work applies ESBMC or CBMC to lwIP's string helpers.

Two honest caveats: absence from search results is not proof of absence,
and the history check here used a shallow clone, so it could not search
prior commits. What *is* established is that both defects are present in
current master (`d08f477`, 2026-09-01).

## Reproducing all of it

```bash
git clone --depth 1 https://github.com/lwip-tcpip/lwip
# extract the four helpers from src/core/def.c into lwip_str.c, then:
veripp scan lwip_str.c
veripp verify lwip_str.c --function lwip_strnstr --repro repro.c
cc -g -fsanitize=address -o repro repro.c lwip_str.c && ./repro
```
