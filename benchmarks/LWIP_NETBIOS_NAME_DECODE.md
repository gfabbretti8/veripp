# Unbounded read of a UDP datagram in lwIP's NetBIOS responder

**Status: not reported upstream.** Internal record.

| | |
|---|---|
| Project | [lwIP](https://github.com/lwip-tcpip/lwip) 2.2.0, `src/apps/netbiosns/netbiosns.c` |
| Entry point | `netbiosns_recv` — a UDP receive callback on port 137 |
| Function | `netbiosns_name_decode`, `netbiosns.c:243` |
| Found by | `veripp` targeting, then read by hand |
| Confirmed by | AddressSanitizer |
| Class | CWE-125 out-of-bounds read, unbounded |
| Trigger | one UDP datagram, 50 bytes, no prior state, no authentication |
| Writes | none — the write side is correctly bounded |

The decoder takes the length of its output buffer and throws it away. It
bounds writes by a hardcoded constant, and bounds reads by nothing at all.

---

## The defect

```c
static int
netbiosns_name_decode(const char *name_enc, char *name_dec, int name_dec_len)
{
  const char *pname;
  char       cname;
  char       cnbname;
  int        idx = 0;

  LWIP_UNUSED_ARG(name_dec_len);          /* <-- the length, discarded */

  pname = name_enc;
  for (;;) {                              /* <-- no bound */
    cname = *pname;
    if (cname == '\0') break;             /* stops only on NUL, */
    if (cname == '.')  break;             /* a scope separator, */
    if (!lwip_isupper(cname)) return -1;  /* or a byte outside A-Z */
    cname -= 'A';
    cnbname = cname << 4;
    pname++;

    cname = *pname;
    if (!lwip_isupper(cname)) return -1;
    cname -= 'A';
    cnbname |= cname;
    pname++;

    if (idx < NETBIOS_NAME_LEN) {         /* the WRITE is bounded */
      name_dec[idx++] = (cnbname != ' ' ? cnbname : '\0');
    }
  }
  return 0;
}
```

NetBIOS first-level encoding maps each name byte to two characters in
`A`–`P`, so a 16-byte name is 32 characters. After 16 pairs `idx` reaches
`NETBIOS_NAME_LEN` and the function stops *storing* — and keeps *reading*.
The loop has no counter, no end pointer, and no use for the one parameter
that would give it either.

## What the caller guarantees

```c
static void
netbiosns_recv(void *arg, struct udp_pcb *upcb, struct pbuf *p, ...)
{
  char netbios_name[NETBIOS_NAME_LEN + 1];
  struct netbios_hdr          *netbios_hdr          = (struct netbios_hdr *)p->payload;
  struct netbios_question_hdr *netbios_question_hdr = (struct netbios_question_hdr *)(netbios_hdr + 1);

  /* is the packet long enough (we need the header in one piece) */
  if (p->len < (sizeof(struct netbios_hdr) + sizeof(struct netbios_question_hdr))) {
    pbuf_free(p);
    return;
  }
  ...
      netbiosns_name_decode((char *)(netbios_question_hdr->encname),
                            netbios_name, sizeof(netbios_name));
```

One length check, on `p->len`, and it is never passed down. `sizeof(netbios_hdr)`
is 12 and `sizeof(netbios_question_hdr)` is 38, so a 50-byte datagram is
accepted — and every byte of it is the attacker's.

`encname` is 33 bytes. Filling it with `A` runs the decoder past it into
`type`, `cls`, and then past the question header entirely. Those are the
attacker's bytes too.

## Reproduction

`benchmarks/repro/netbios_decode.c` — the function verbatim, given a
datagram of exactly the minimum length `netbiosns_recv` accepts:

```c
char *packet = (char *)malloc(MIN_PACKET);   /* 50 */
memset(packet, 'A', MIN_PACKET);             /* every byte a legal name char */
netbiosns_name_decode(packet + HDR_LEN + 1, name_dec, sizeof(name_dec));
```

```
datagram 50 bytes (the minimum netbiosns_recv accepts)
decoding from offset 13, 37 bytes remain in the packet
==79236==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x6060000004d2
READ of size 1 at 0x6060000004d2 thread T0
    #0 netbiosns_name_decode netbios_decode.c:46
    #1 main netbios_decode.c:78
0x6060000004d2 is located 0 bytes after 50-byte region [0x6060000004a0,0x6060000004d2)
```

## How far it runs

The loop stops at the first byte outside `A`–`Z`. That is the only bound,
and it is a property of the *adjacent memory*, not of the packet.
`benchmarks/repro/netbios_far.c` instruments the pointer and puts 4 KiB of
uppercase bytes after the datagram:

```
datagram was 50 bytes; the decoder read 4133 bytes from encname,
i.e. 4096 bytes past the end of the datagram
```

Nothing in the function would have stopped it at 4 MiB either.

## Reaching past the pbuf in a real stack — measured, and it corrects this report

An earlier revision of this section argued that the read leaves its
allocation "on the attacker's schedule": pool elements are contiguous, the
stale bytes are previous datagrams the attacker also sent, so filling memory
with `A` first was a matter of sending more queries. That reasoning was
labelled as reasoning rather than measurement. It has now been measured, and
**it was wrong.**

`benchmarks/repro/netbios_pbuf.c` links lwIP's own `pbuf.c`, `mem.c` and
`memp.c` from the tree and allocates the datagram with `pbuf_alloc`.

**Default configuration** (lwIP's static heap, `MEM_LIBC_MALLOC=0`):

```
  one query  decode read 38 bytes from encname; the datagram had 37 left
  groomed    8 queries in flight, first payload=0x102694054
  groomed    decode read 38 bytes from encname; the datagram had 37 left
  PBUF_POOL  decode read 38 bytes from encname; the datagram had 37 left
  bytes immediately after the datagram: 00 40 00 00 00 00 00 00
```

**Exactly one byte past, in every case.** The byte after the payload is the
next block's `struct mem` header, and it is zero, so the walk stops there.
Eight queries in flight changes nothing: lwIP interleaves that metadata
between every pair of allocations, so a run of attacker bytes cannot span
two blocks. The grooming argument does not survive contact with the
allocator.

**With `MEM_LIBC_MALLOC=1` and `MEMP_MEM_MALLOC=1`** — both documented lwIP
options, for platforms with a real heap — each pbuf is its own allocation
and there is no lwIP metadata after it:

```
==587==ERROR: AddressSanitizer: heap-buffer-overflow
READ of size 1 at 0x60c000000240 thread T0
    #0 netbiosns_name_decode netbios_pbuf.c:33
0x60c000000240 is located 0 bytes after 128-byte region [0x60c0000001c0,0x60c000000240)
allocated by thread T0 here:
    #1 mem_malloc mem.c:209
    #2 pbuf_alloc pbuf.c:284
```

That is a genuine out-of-bounds read past a real pbuf, in a supported
configuration. How far it then runs depends on what the platform heap put
next, which is not lwIP's to control and not demonstrated here.

The 4096-byte figure below describes a synthetic layout — a single
allocation filled with uppercase bytes — that lwIP's own allocators do not
produce. It shows that the loop has no bound. It does not show that lwIP
reaches that far.

## Severity

* **Remote, unauthenticated, no prior state.** One UDP datagram to port 137.
  The over-read happens during decoding, before the name is compared against
  the local name, so it does not depend on the query matching anything.
* **Read-only.** `idx < NETBIOS_NAME_LEN` bounds the writes into a
  17-byte buffer correctly. There is no corruption.
* **The loop is unbounded; the measured over-read is one byte.** Those are
  different claims and the second is the one that survives measurement in
  lwIP's default allocators. In a `MEM_LIBC_MALLOC` build the read leaves
  the allocation, which ASan confirms; how far it goes then is the platform
  heap's business. The "walks into unmapped space" reading is not supported
  by anything measured here and should not be repeated.
* **Not an information leak by itself.** Only the first 16 decoded bytes are
  kept, and the response echoes `encname` from the packet
  (`MEMCPY(resp->query_name, netbios_question_hdr->encname, 33)`), which is
  in bounds. What is read past the end influences only when the loop stops.
* **Requires the application to start the responder.** `netbiosns_init()` is
  opt-in, and binds UDP 137. It ships in lwIP's contrib examples and is
  commonly enabled on devices that want to be reachable by NetBIOS name from
  Windows hosts.

## Still present upstream

Checked against `lwip-tcpip/lwip` `master` (fetched 2026-09-05), not just the
2.2.0 tarball this hunt used. Unchanged: the same unbounded `for (;;)`, the
same `LWIP_UNUSED_ARG(name_dec_len)`, and the same single length check in
`netbiosns_recv` that validates only that the datagram is long enough to
hold the two headers.

A search of the CVE databases and lwIP's advisories for
`netbiosns_name_decode` turned up nothing -- the only recent lwIP CVE that
surfaced was CVE-2026-8836, which is SNMPv3. That is weak evidence: absence
from a search is not absence of a report, and the bug may be known and
unfixed rather than unknown. What it does establish is that the code in
`master` today has the defect.

## Suggested fix

The function already takes the length it needs to be given, and a bound on
the encoded side is the one that matters:

```c
 static int
-netbiosns_name_decode(const char *name_enc, char *name_dec, int name_dec_len)
+netbiosns_name_decode(const char *name_enc, char *name_dec, int name_dec_len,
+                      int name_enc_len)
 {
-  LWIP_UNUSED_ARG(name_dec_len);
+  const char *pend = name_enc + name_enc_len;
   ...
-  for (;;) {
+  for (; pname + 1 < pend; ) {
```

with the caller passing what it already knows:

```c
netbiosns_name_decode((char *)(netbios_question_hdr->encname), netbios_name,
                      sizeof(netbios_name),
                      (int)(p->len - (sizeof(struct netbios_hdr) + 1)));
```

`name_dec_len` should also replace the hardcoded `NETBIOS_NAME_LEN`, so the
two buffers are both described by their own parameters.

## Why no fuzzer found it

lwIP's `test/fuzz` drives an ethernet frame through
`netif -> ip4 -> udp/tcp`, and its `lwipopts.h` enables
`LWIP_MDNS_RESPONDER` and `LWIP_SNMP` — so those two app protocols *are*
reachable from the fuzzer, and this one is not. `netbiosns` is never
initialised in that build, so nothing binds port 137 and no datagram ever
reaches `netbiosns_recv`.

That is the fifth time in this hunt that a defect has been sitting next to
fuzzed code in a module the fuzzer does not build, and the first where the
distinction was checked *before* choosing the target rather than noticed
afterwards: the fuzz configuration was read first, mdns and snmp were ruled
out because it enables them, and netbiosns was picked because it does not.
