/* tinyusb host examples: USB string descriptor -> UTF-8 conversion.
 *
 * examples/host/bare_api/src/main.c (also device_info, and
 * examples/dual/host_info_to_device_cdc), verbatim:
 *
 *   CFG_TUH_MEM_SECTION uint16_t temp_buf[128];      // 256 bytes
 *
 *   static void print_utf16(uint16_t *buf, size_t buf_len) {
 *     if ((buf[0] & 0xff) == 0) return;
 *     size_t utf16_len = ((buf[0] & 0xff) - 2) / sizeof(uint16_t);
 *     size_t utf8_len  = _count_utf8_bytes(buf + 1, utf16_len);
 *     _convert_utf16le_to_utf8(buf + 1, utf16_len, (uint8_t *) buf,
 *                              sizeof(uint16_t) * buf_len);
 *     ((uint8_t *) buf)[utf8_len] = '\0';
 *
 * buf[0] is the string descriptor's bLength/bDescriptorType word, which the
 * attached DEVICE supplies. Two things follow from that.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

/* verbatim */
static int _count_utf8_bytes(const uint16_t *buf, size_t len) {
  size_t total_bytes = 0;
  for (size_t i = 0; i < len; i++) {
    uint16_t chr = buf[i];
    if (chr < 0x80)       total_bytes += 1;
    else if (chr < 0x800) total_bytes += 2;
    else                  total_bytes += 3;
  }
  return (int) total_bytes;
}

/* verbatim, including the TODO and the discarded bound */
static void _convert_utf16le_to_utf8(const uint16_t *utf16, size_t utf16_len,
                                     uint8_t *utf8, size_t utf8_len) {
  // TODO: Check for runover.
  (void) utf8_len;
  for (size_t i = 0; i < utf16_len; i++) {
    uint16_t chr = utf16[i];
    if (chr < 0x80) {
      *utf8++ = chr & 0xffu;
    } else if (chr < 0x800) {
      *utf8++ = (uint8_t) (0xC0 | (chr >> 6 & 0x1F));
      *utf8++ = (uint8_t) (0x80 | (chr >> 0 & 0x3F));
    } else {
      *utf8++ = (uint8_t) (0xE0 | (chr >> 12 & 0x0F));
      *utf8++ = (uint8_t) (0x80 | (chr >> 6 & 0x3F));
      *utf8++ = (uint8_t) (0x80 | (chr >> 0 & 0x3F));
    }
  }
}

static void print_utf16(uint16_t *buf, size_t buf_len) {
  if ((buf[0] & 0xff) == 0) return;
  size_t utf16_len = ((buf[0] & 0xff) - 2) / sizeof(uint16_t);
  size_t utf8_len = (size_t) _count_utf8_bytes(buf + 1, utf16_len);
  printf("  bLength=%u -> utf16_len=%zu, utf8 output would be %zu bytes "
         "into a %zu-byte buffer\n",
         (unsigned)(buf[0] & 0xff), utf16_len, utf8_len,
         sizeof(uint16_t) * buf_len);
  fflush(stdout);
  _convert_utf16le_to_utf8(buf + 1, utf16_len, (uint8_t *) buf,
                           sizeof(uint16_t) * buf_len);
  ((uint8_t *) buf)[utf8_len] = '\0';
}

int main(int argc, char **argv) {
  const int mode = (argc > 1) ? atoi(argv[1]) : 2;
  /* the example's buffer, heap-allocated so ASan can see its edge */
  uint16_t *temp_buf = (uint16_t *) malloc(128 * sizeof(uint16_t));

  for (int i = 1; i < 128; i++) temp_buf[i] = 0x0800;   /* 3 UTF-8 bytes each */

  if (mode == 1) {
    puts("mode 1: bLength = 1  (the subtraction underflows)");
    temp_buf[0] = 0x0301;                                /* bLength = 1 */
  } else {
    puts("mode 2: bLength = 255 (maximum a descriptor may claim)");
    temp_buf[0] = 0x03ff;                                /* bLength = 255 */
  }

  print_utf16(temp_buf, 128);
  puts("returned without a detected overrun");
  free(temp_buf);
  return 0;
}
