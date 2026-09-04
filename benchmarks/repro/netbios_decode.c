/* lwIP netbiosns_name_decode(): unbounded read of a UDP packet.
 *
 * src/apps/netbiosns/netbiosns.c
 *
 * netbiosns_recv() checks only that the datagram is long enough to hold the
 * two headers:
 *
 *   if (p->len < (sizeof(struct netbios_hdr) + sizeof(struct netbios_question_hdr)))
 *       { pbuf_free(p); return; }
 *
 * and then hands netbiosns_name_decode() a pointer to encname. The decode
 * loop takes a name_dec_len -- and discards it with LWIP_UNUSED_ARG. It
 * bounds the WRITE by a hardcoded NETBIOS_NAME_LEN, and bounds the READ by
 * nothing at all: `for (;;)`, two bytes per iteration, stopping only at a
 * byte that is NUL, '.', or outside A-Z.
 *
 * Every one of those bytes is attacker-controlled.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define NETBIOS_NAME_LEN 16
static int lwip_isupper(int c) { return c >= 'A' && c <= 'Z'; }

/* verbatim from netbiosns.c:243 */
static int netbiosns_name_decode(const char *name_enc, char *name_dec, int name_dec_len)
{
  const char *pname;
  char       cname;
  char       cnbname;
  int        idx = 0;

  (void)name_dec_len;                    /* LWIP_UNUSED_ARG */

  pname = name_enc;
  for (;;) {
    cname = *pname;
    if (cname == '\0') break;
    if (cname == '.')  break;
    if (!lwip_isupper(cname)) return -1;
    cname -= 'A';
    cnbname = cname << 4;
    pname++;

    cname = *pname;
    if (!lwip_isupper(cname)) return -1;
    cname -= 'A';
    cnbname |= cname;
    pname++;

    if (idx < NETBIOS_NAME_LEN) {
      name_dec[idx++] = (cnbname != ' ' ? cnbname : '\0');
    }
  }
  return 0;
}

/* sizeof(netbios_hdr) + sizeof(netbios_question_hdr), packed */
#define HDR_LEN       12
#define QUESTION_LEN  (1 + 33 + 2 + 2)
#define MIN_PACKET    (HDR_LEN + QUESTION_LEN)   /* 50: all the caller checks */

int main(void)
{
    char name_dec[NETBIOS_NAME_LEN + 1];

    /* A datagram of exactly the minimum length the handler accepts. */
    char *packet = (char *)malloc(MIN_PACKET);
    memset(packet, 'A', MIN_PACKET);       /* every byte a legal name char */

    const char *encname = packet + HDR_LEN + 1;   /* question_hdr->encname */
    printf("datagram %d bytes (the minimum netbiosns_recv accepts)\n", MIN_PACKET);
    printf("decoding from offset %d, %d bytes remain in the packet\n",
           HDR_LEN + 1, (int)(MIN_PACKET - (HDR_LEN + 1)));
    fflush(stdout);

    netbiosns_name_decode(encname, name_dec, sizeof(name_dec));

    printf("returned without reading past the end\n");
    free(packet);
    return 0;
}
