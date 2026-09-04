/* How far past the datagram can the read run?
 * The loop stops at the first byte outside A-Z. Nothing else bounds it.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define NETBIOS_NAME_LEN 16
static int lwip_isupper(int c) { return c >= 'A' && c <= 'Z'; }
static const char *g_start; static long g_max;

static int netbiosns_name_decode(const char *name_enc, char *name_dec, int name_dec_len)
{
  const char *pname; char cname, cnbname; int idx = 0;
  (void)name_dec_len;
  pname = name_enc;
  for (;;) {
    if (pname - g_start > g_max) g_max = pname - g_start;   /* instrument only */
    cname = *pname;
    if (cname == '\0') break;
    if (cname == '.')  break;
    if (!lwip_isupper(cname)) return -1;
    cname -= 'A'; cnbname = cname << 4; pname++;
    if (pname - g_start > g_max) g_max = pname - g_start;
    cname = *pname;
    if (!lwip_isupper(cname)) return -1;
    cname -= 'A'; cnbname |= cname; pname++;
    if (idx < NETBIOS_NAME_LEN) name_dec[idx++] = (cnbname != ' ' ? cnbname : '\0');
  }
  return 0;
}

int main(void)
{
    char name_dec[NETBIOS_NAME_LEN + 1];
    /* A 50-byte datagram, then 4 KiB of adjacent memory that happens to be
     * uppercase ASCII -- another NetBIOS packet, a hostname table, HTTP
     * headers, a previous request still in the pool. */
    const int packet = 50, adjacent = 4096;
    char *region = (char *)malloc(packet + adjacent);
    memset(region, 'A', packet + adjacent);
    region[packet + adjacent - 1] = 0;      /* something eventually stops it */

    g_start = region + 13; g_max = 0;
    netbiosns_name_decode(region + 13, name_dec, sizeof(name_dec));

    printf("datagram was %d bytes; the decoder read %ld bytes from encname,\n"
           "i.e. %ld bytes past the end of the datagram\n",
           packet, g_max + 1, (g_max + 1) - (packet - 13));
    free(region);
    return 0;
}
