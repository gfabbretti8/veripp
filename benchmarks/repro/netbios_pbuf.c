/* Does the NetBIOS over-read leave a real lwIP pbuf?
 *
 * The earlier reproduction modelled the datagram as an exactly-sized
 * malloc. This one uses lwIP's own pbuf allocator, compiled from the tree,
 * and asks the question directly.
 *
 * netbiosns_name_decode is copied verbatim from netbiosns.c:243 rather than
 * linked, because it is static and its file drags in the whole UDP stack.
 * What is real here is the pbuf.
 */
#include <stdio.h>
#include <string.h>
#include "lwip/opt.h"
#include "lwip/pbuf.h"
#include "lwip/mem.h"
#include "lwip/memp.h"

#define NETBIOS_NAME_LEN 16
static int nb_isupper(int c) { return c >= 'A' && c <= 'Z'; }

/* verbatim from src/apps/netbiosns/netbiosns.c:243 */
static const char *g_start; static long g_far;
static int netbiosns_name_decode(const char *name_enc, char *name_dec, int name_dec_len)
{
  const char *pname; char cname, cnbname; int idx = 0;
  (void)name_dec_len;                      /* LWIP_UNUSED_ARG */
  pname = name_enc;
  g_start = name_enc; g_far = 0;
  for (;;) {
    if (pname - g_start > g_far) g_far = pname - g_start;
    cname = *pname;
    if (cname == '\0') break;
    if (cname == '.')  break;
    if (!nb_isupper(cname)) return -1;
    cname -= 'A'; cnbname = cname << 4; pname++;
    if (pname - g_start > g_far) g_far = pname - g_start;
    cname = *pname;
    if (!nb_isupper(cname)) return -1;
    cname -= 'A'; cnbname |= cname; pname++;
    if (idx < NETBIOS_NAME_LEN) name_dec[idx++] = (cnbname != ' ' ? cnbname : '\0');
  }
  return 0;
}

#define MIN_PACKET 50            /* sizeof(netbios_hdr) + sizeof(netbios_question_hdr) */

static void probe(pbuf_layer layer, pbuf_type type, const char *what)
{
  char name_dec[NETBIOS_NAME_LEN + 1];
  struct pbuf *p = pbuf_alloc(layer, MIN_PACKET, type);
  if (p == NULL) { printf("  %-10s pbuf_alloc failed\n", what); return; }

  /* a NetBIOS name query whose every byte is a legal encoded-name char */
  memset(p->payload, 'A', p->len);

  printf("  %-10s p->len=%u  payload=%p\n", what, (unsigned)p->len, p->payload);
  fflush(stdout);

  netbiosns_name_decode(((const char *)p->payload) + 13, name_dec, sizeof(name_dec));

  printf("  %-10s decode read %ld bytes from encname; the datagram had %d left\n",
         what, g_far + 1, MIN_PACKET - 13);
  pbuf_free(p);
}

/* Several queries in flight, which is what an attacker sending more than one
 * packet produces: the bytes after each datagram are the next datagram, and
 * those are the attacker's too. */
static void probe_groomed(void)
{
  char name_dec[NETBIOS_NAME_LEN + 1];
  struct pbuf *chain[8];
  int i;

  for (i = 0; i < 8; i++) {
    chain[i] = pbuf_alloc(PBUF_TRANSPORT, MIN_PACKET, PBUF_RAM);
    if (chain[i] == NULL) { printf("  groomed   alloc %d failed\n", i); return; }
    memset(chain[i]->payload, 'A', chain[i]->len);
  }
  printf("  groomed   8 queries in flight, first payload=%p\n", chain[0]->payload);
  fflush(stdout);

  netbiosns_name_decode(((const char *)chain[0]->payload) + 13, name_dec, sizeof(name_dec));

  printf("  groomed   decode read %ld bytes from encname; the datagram had %d left\n",
         g_far + 1, MIN_PACKET - 13);
  for (i = 0; i < 8; i++) pbuf_free(chain[i]);
}

int main(void)
{
  mem_init();
  memp_init();
  printf("MEM_LIBC_MALLOC=%d MEMP_MEM_MALLOC=%d\n", MEM_LIBC_MALLOC, MEMP_MEM_MALLOC);
  probe(PBUF_TRANSPORT, PBUF_RAM, "one query");
  probe_groomed();
  probe(PBUF_TRANSPORT, PBUF_POOL, "PBUF_POOL");
  {   /* what actually sits immediately after the datagram? */
    struct pbuf *q = pbuf_alloc(PBUF_TRANSPORT, MIN_PACKET, PBUF_RAM);
    const unsigned char *after = ((const unsigned char *)q->payload) + q->len;
    int i;
    memset(q->payload, 'A', q->len);
    printf("  bytes immediately after the datagram: ");
    for (i = 0; i < 8; i++) printf("%02x ", after[i]);
    printf("\n");
    pbuf_free(q);
  }
  return 0;
}
