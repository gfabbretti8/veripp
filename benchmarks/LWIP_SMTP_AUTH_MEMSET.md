# A hardcoded 64 against a configurable buffer in lwIP's SMTP client

**Status: not reported upstream.** Internal record.

| | |
|---|---|
| Project | [lwIP](https://github.com/lwip-tcpip/lwip) 2.2.0, `src/apps/smtp/smtp.c` |
| Function | `smtp_set_auth`, first statement |
| Confirmed by | AddressSanitizer, **and the compiler** |
| Class | CWE-787 out-of-bounds **write** |
| Trigger | calling `smtp_set_auth()` in a build that tunes two documented options |
| Remote | **no** — this fires at initialisation, not from the network |

A write, unlike most of this report. It is also the least interesting kind of
write, because a compiler with fortify enabled diagnoses it without running.

---

## The defect

```c
/* smtp.c:291 */
static char smtp_auth_plain[SMTP_MAX_USERNAME_LEN + SMTP_MAX_PASS_LEN + 3];

/* smtp.c, first statement of smtp_set_auth() */
memset(smtp_auth_plain, 0xfa, 64);
```

The buffer's size is a sum of two options. The `memset` is a literal 64.

`SMTP_MAX_USERNAME_LEN` and `SMTP_MAX_PASS_LEN` are documented in
`smtp_opts.h` and default to 32 each, making the buffer 67 bytes — so at the
defaults the `memset` fits, with three bytes to spare. That is the only
reason this is not a bug in every build.

Lowering them overflows. Lowering them is the entire point of their being
options on a stack for constrained devices.

The `0xfa` fill is scaffolding: nothing reads it. The next statements set
`*smtp_auth_plain = 0` and `strcpy` the credentials in.

## Reproduction

`benchmarks/repro/smtp_memset.c`, with the two options at 16 rather than 32:

```
SMTP_MAX_USERNAME_LEN=16 SMTP_MAX_PASS_LEN=16
sizeof(smtp_auth_plain) = 35, memset writes 64
==82269==ERROR: AddressSanitizer: global-buffer-overflow
WRITE of size 64 at 0x000102088203 thread T0
0x000102088203 is located 0 bytes after global variable 'smtp_auth_plain' of size 35
```

29 bytes of `0xfa` past the end of a static buffer, into whatever the linker
put next.

## Severity, and why it is lower than a write usually is

* **Not remote.** `smtp_set_auth()` is called by the application with its own
  credentials, normally once at startup. Nothing on the network reaches it.
* **Not default.** It needs `SMTP_MAX_USERNAME_LEN + SMTP_MAX_PASS_LEN < 61`.
* **The compiler already says so.** Building the reproduction produces

  ```
  warning: 'memset' will always overflow; destination buffer has size 35,
  but size argument is 64 [-Wfortify-source]
  ```

  Anyone compiling that configuration with fortify enabled is told at build
  time. Embedded toolchains frequently do not enable it, and warnings in a
  large build are easy to lose, but this is not a silent defect.
* **Deterministic and total.** When it does fire it always writes the same
  29 bytes past the end, at startup, corrupting adjacent globals for the
  lifetime of the process.

The honest summary is that this is a latent configuration hazard rather than
an attack. It is recorded because it is a real out-of-bounds write with a
one-line fix, and because the shape — a literal size next to a computed
buffer — is worth looking for elsewhere.

## Suggested fix

The `memset` serves no purpose and can go. If the intent was to poison the
buffer:

```c
-  memset(smtp_auth_plain, 0xfa, 64);
+  memset(smtp_auth_plain, 0, sizeof(smtp_auth_plain));
```

## How it was found

By grepping the modules lwIP's fuzzer does not build for
`LWIP_UNUSED_ARG` applied to a length parameter — the shape that had just
produced the NetBIOS finding. `smtp_base64_encode` discards a `target_len`
and bounds its writes with an assertion instead, which is compiled out in
release builds; that turned out to be safe at every call site, because the
credential setter validates both lengths. The `memset` two functions up was
in the diff while checking that.
