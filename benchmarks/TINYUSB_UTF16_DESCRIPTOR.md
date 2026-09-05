# A USB string descriptor overflows the buffer it is converted in

**Status: not reported upstream.** Internal record.

| | |
|---|---|
| Project | [TinyUSB](https://github.com/hathach/tinyusb), `examples/host/bare_api`, `examples/host/device_info`, `examples/dual/host_info_to_device_cdc` |
| Functions | `print_utf16`, `_convert_utf16le_to_utf8`, `_count_utf8_bytes` |
| Reached from | a USB **string descriptor**, supplied by the attached device |
| Confirmed by | AddressSanitizer |
| Class | CWE-787 out-of-bounds **write**, and CWE-191 integer underflow |
| Writes | **yes** — 122 bytes past a 256-byte buffer |
| Scope | example code, not the TinyUSB library core. See [Severity](#severity). |

This report's first attacker-driven out-of-bounds **write**. One byte of a
USB descriptor decides how far it goes.

---

## The defect

```c
CFG_TUH_MEM_SECTION uint16_t temp_buf[128];          /* 256 bytes */

static void print_utf16(uint16_t *buf, size_t buf_len) {
  if ((buf[0] & 0xff) == 0) return;                       /* empty */
  size_t utf16_len = ((buf[0] & 0xff) - 2) / sizeof(uint16_t);
  size_t utf8_len  = (size_t) _count_utf8_bytes(buf + 1, utf16_len);
  _convert_utf16le_to_utf8(buf + 1, utf16_len, (uint8_t *) buf,
                           sizeof(uint16_t) * buf_len);
  ((uint8_t *) buf)[utf8_len] = '\0';
  printf("%s", (char *) buf);
}
```

`buf[0] & 0xff` is `bLength` from the string descriptor the **device** sent.
Two separate faults follow from it.

### 1. The length is converted with the bound thrown away

```c
static void _convert_utf16le_to_utf8(const uint16_t *utf16, size_t utf16_len,
                                     uint8_t *utf8, size_t utf8_len) {
  // TODO: Check for runover.
  (void) utf8_len;
```

UTF-8 takes up to **three** bytes per UTF-16 unit while the input takes two,
so the output can be 1.5× the input. The conversion runs in place, in the
same 256-byte buffer. With `bLength = 255`, `utf16_len` is 126, and 126
characters at or above `U+0800` produce 378 bytes.

The function is handed the destination size and the code says, in a comment,
that it does not use it.

### 2. `bLength < 2` underflows

`(buf[0] & 0xff) - 2` is `int` arithmetic; dividing by `sizeof(uint16_t)`
converts it to `size_t` first. A device claiming `bLength = 1` gives

```
utf16_len = (size_t)(-1) / 2 = 9223372036854775807
```

and `_count_utf8_bytes` walks it. The `bLength == 0` case is rejected on the
line above; `1` is not.

## Reproduction

`benchmarks/repro/tinyusb_utf16.c`, all three functions verbatim, on a
128-entry buffer.

**`bLength = 255`, characters at U+0800 — the write:**

```
  bLength=255 -> utf16_len=126, utf8 output would be 378 bytes into a 256-byte buffer
==53784==ERROR: AddressSanitizer: heap-buffer-overflow
WRITE of size 1 at 0x611000000140 thread T0
    #0 _convert_utf16le_to_utf8 tinyusb_utf16.c:50
    #1 print_utf16 tinyusb_utf16.c:65
0x611000000140 is located 0 bytes after 256-byte region [0x611000000040,0x611000000140)
```

122 bytes past the end, and the content is derived from the descriptor.

**`bLength = 1` — the underflow:**

```
==53815==ERROR: AddressSanitizer: heap-buffer-overflow
READ of size 2 at 0x611000000140 thread T0
    #0 _count_utf8_bytes tinyusb_utf16.c:28
    #1 print_utf16 tinyusb_utf16.c:59
```

## Still present upstream

Checked `hathach/tinyusb` `master` (fetched 2026-09-05). Both are unchanged:
`utf16_len` is still `((buf[0] & 0xff) - 2) / sizeof(uint16_t)` with no
`bLength >= 2` check, and `_convert_utf16le_to_utf8` still opens with
`// TODO: Check for runover.` and `(void) utf8_len;`.

## Severity

Stated carefully, because the scope is the part that limits it.

* **It is a write**, which nothing else in this report's lwIP findings was.
  The allocator cannot mitigate a write the way it caps a read: 122 bytes of
  attacker-derived UTF-8 land in whatever follows the buffer.
* **The attacker is a USB device.** Physical access, or a device already
  attached. That is the standard threat model for a host stack — enumerating
  hostile hardware is the job.
* **No authentication and no prior state.** String descriptors are read
  during enumeration, before anything trusts the device.
* **`temp_buf` is a static in `CFG_TUH_MEM_SECTION`**, a DMA-capable region
  on most ports. What follows it is other driver state, not a stack frame,
  so this is adjacent-object corruption rather than a return address.
* **It is example code.** `examples/host/bare_api`, `examples/host/device_info`
  and `examples/dual/host_info_to_device_cdc` each carry their own copy. The
  TinyUSB *library* is not affected. These examples are the documented
  starting point for a host application and the copy is verbatim in all
  three, which is why it is worth recording, but "TinyUSB is vulnerable"
  would be the wrong sentence.

## Suggested fix

```c
 static void print_utf16(uint16_t *buf, size_t buf_len) {
-  if ((buf[0] & 0xff) == 0) return;
-  size_t utf16_len = ((buf[0] & 0xff) - 2) / sizeof(uint16_t);
+  const size_t bLength = buf[0] & 0xff;
+  if (bLength < 2) return;
+  size_t utf16_len = (bLength - 2) / sizeof(uint16_t);
```

and in the converter, honour the parameter it is given:

```c
-  // TODO: Check for runover.
-  (void) utf8_len;
+  const uint8_t *const end = utf8 + utf8_len;
```

with each branch checking `end - utf8` before writing. Converting in place
into the same buffer is worth revisiting separately: with a 1.5× expansion
the writer passes the reader after the third character even when the buffer
is large enough.

## How it was found

By the unused-length check, run across seven libraries at once. It reports a
length-shaped parameter that a function never reads, when that parameter
pairs with a buffer the body does use — 44 hits in TinyUSB, of which
`_convert_utf16le_to_utf8(utf8_len)` appeared three times.

That check exists because three earlier findings had the same shape. This is
the fourth, and the first found by the check rather than by a person
grepping for it.
