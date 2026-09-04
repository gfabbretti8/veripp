# `%.*v` calls strlen on the counted field it was given a length for

**Status: not reported upstream.** Internal record.

| | |
|---|---|
| Project | [lwIP](https://github.com/lwip-tcpip/lwip) 2.2.0 **and `master`**, `src/netif/ppp/utils.c` |
| Function | `ppp_vslprintf`, `case 'v'` / `case 'q'` |
| Reached from | PAP, CHAP and EAP packet handling — **before authentication completes** |
| Confirmed by | AddressSanitizer |
| Class | CWE-125 out-of-bounds read, unbounded |
| Writes | none |

The conversion exists because the bytes are a counted field rather than a C
string. It calls `strlen` on them anyway.

---

## The defect

```c
	case 'v':		/* "visible" string */
	case 'q':		/* quoted string */
	    quoted = c == 'q';
	    p = va_arg(args, unsigned char *);
	    if (p == NULL)
		p = (const unsigned char *)"<NULL>";
	    if (fillch == '0' && prec >= 0) {
		n = prec;
	    } else {
		n = strlen((const char *)p);     /* <-- unbounded */
		if (prec >= 0 && n > prec)
		    n = prec;
	    }
```

`fillch` is `'0'` only when the format begins `%0`. Every call site in the
tree writes `%.*v` or `%.*q`, so `fillch` is `' '`, the `else` runs, and the
length that was passed is used only to clamp a result `strlen` has already
gone and computed.

## Where the bytes come from

Seven call sites, each lifting a counted field straight out of a packet:

```c
chap-new.c:319   ppp_slprintf(rname, sizeof(rname), "%.*v", len, name);
chap-new.c:460   ppp_slprintf(rname, sizeof(rname), "%.*v", nlen, pkt + clen + 1);
chap-new.c:522   ppp_info(("%s: %.*v", msg, len, pkt));
chap_ms.c:486    ppp_error(("Unknown MS-CHAP authentication failure: %.*v", ...));
eap.c:1362       ppp_info(("EAP: Identity prompt \"%.*q\"", len, inp));
eap.c:1396       ppp_info(("EAP: Notification \"%.*q\"", len, inp));
upap.c:438       ppp_slprintf(rhostname, sizeof(rhostname), "%.*v", ruserlen, ruser);
```

`upap.c:438` is the PAP server formatting the peer's username. `chap-new.c`
handles a received Challenge and a received Response. All of it runs while
the link is still unauthenticated — which is the point of an authentication
protocol.

The comment above two of them reads *"Null terminate and clean remote
name."* The bytes are not NUL-terminated; that is why a length is passed.

## Reproduction

`benchmarks/repro/ppp_slprintf_v.c` — the `'v'` path faithful to `utils.c`,
given an 8-byte username field with no terminator, and the length passed as
the precision exactly as `upap.c` passes it:

```
username field is 8 bytes, not NUL-terminated
caller passes that length as the precision: "%.*v"
==94728==ERROR: AddressSanitizer: heap-buffer-overflow
READ of size 9 at 0x6020000000d8 thread T0
    #0 strlen
    #1 vslprintf_v ppp_slprintf_v.c:42
0x6020000000d8 is located 0 bytes after 8-byte region [0x6020000000d0,0x6020000000d8)
```

## Still present upstream

Checked `lwip-tcpip/lwip` `master` (fetched 2026-09-05). The `case 'v'`
block is unchanged: `strlen` first, clamp after.

## Severity

* **Remote and pre-authentication.** PAP, CHAP and EAP are what run *before*
  a peer is trusted. No credentials are needed to send the packet that
  reaches this.
* **Not opt-in the way NetBIOS is.** PPP with PAP or CHAP is the normal
  configuration for a cellular or dial-up link, not an extra app a
  developer chooses to start.
* **Read-only, and it does not leak.** `n` is clamped to `prec` immediately
  afterwards, so nothing beyond the field reaches the formatted output. What
  escapes the bounds is the `strlen` scan itself.
* **Unbounded in principle, bounded by luck in practice.** `strlen` stops at
  the first zero byte. The field is usually at the end of the packet, so the
  scan begins immediately in whatever follows it — pool slack, a previous
  packet, or past the allocation. A zero byte usually turns up quickly; the
  point is that nothing guarantees one.
* The realistic outcome is a fault or a hang, not corruption. On an MCU
  without an MMU that means walking into peripheral or unmapped space.

## Suggested fix

The precision is already the right answer; it just needs to be used before
`strlen` rather than after:

```c
-	    if (fillch == '0' && prec >= 0) {
+	    if (prec >= 0) {
 		n = prec;
 	    } else {
 		n = strlen((const char *)p);
-		if (prec >= 0 && n > prec)
-		    n = prec;
 	    }
```

Every in-tree caller passes a precision, so this makes the `strlen` path
reachable only for `%v` with no precision — where the argument really is a C
string.

## How it was found

Not by the solver. `--unterminated` was pointed at the unfuzzed lwIP modules
and produced only contract false positives — `get_secret`, whose `client`
argument is the configured local name; `smtp_set_server_addr`, whose
argument is a configured hostname. Reading one of those false positives
meant reading `chap_respond`, and the line above the call was

```c
	/* Null terminate and clean remote name. */
	ppp_slprintf(rname, sizeof(rname), "%.*v", nlen, pkt + clen + 1);
```

which raises the question of what `ppp_slprintf` does with `nlen`.

Second finding in a row where the tool's contribution was narrowing where to
read rather than producing the counterexample, and the second where a
function was handed a length and did not use it. That shape now has its own
entry in the field report.
