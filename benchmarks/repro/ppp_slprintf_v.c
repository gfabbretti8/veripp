/* lwIP ppp_vslprintf(): the "%.*v" conversion calls strlen() on data it was
 * handed a length for.
 *
 * src/netif/ppp/utils.c, case 'v':
 *
 *     if (fillch == '0' && prec >= 0) {
 *         n = prec;
 *     } else {
 *         n = strlen((const char *)p);        <-- unbounded
 *         if (prec >= 0 && n > prec)
 *             n = prec;
 *     }
 *
 * fillch is '0' only for a format beginning "%0". Every call site in the PPP
 * tree writes "%.*v" or "%.*q", so fillch is ' ' and the strlen runs.
 *
 * The whole point of passing a precision is that the bytes are NOT a C
 * string -- they are a counted field lifted out of a packet:
 *
 *   chap-new.c:319  ppp_slprintf(rname, sizeof(rname), "%.*v", len, name);
 *   chap-new.c:460  ppp_slprintf(rname, sizeof(rname), "%.*v", nlen, pkt + clen + 1);
 *   upap.c:438      ppp_slprintf(rhostname, sizeof(rhostname), "%.*v", ruserlen, ruser);
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>

#define OUTCHAR(c) (buflen > 0 ? (--buflen, *buf++ = (c)) : 0)

/* The 'v' path of ppp_vslprintf, faithful to utils.c */
static int vslprintf_v(char *buf, int buflen, int prec, const unsigned char *p)
{
    int c, n;
    int fillch = ' ';               /* "%.*v" never sets '0' */
    char *buf0 = buf;
    --buflen;

    if (fillch == '0' && prec >= 0) {
        n = prec;
    } else {
        n = (int)strlen((const char *)p);       /* utils.c: the unbounded read */
        if (prec >= 0 && n > prec)
            n = prec;
    }
    while (n > 0 && buflen > 0) {
        c = *p++;
        --n;
        if (c < 0x20 || (0x7f <= c && c < 0xa0)) {
            if (c == '\t') OUTCHAR(c);
            else { OUTCHAR('^'); OUTCHAR(c ^ 0x40); }
        } else {
            OUTCHAR(c);
        }
    }
    *buf = 0;
    return buf - buf0;
}

int main(void)
{
    char rhostname[256];

    /* A PAP Authenticate-Request whose username field runs to the end of the
     * datagram: ruserlen bytes, no terminator, because none is required. */
    const int ruserlen = 8;
    unsigned char *ruser = (unsigned char *)malloc(ruserlen);
    memset(ruser, 'u', ruserlen);

    printf("username field is %d bytes, not NUL-terminated\n", ruserlen);
    printf("caller passes that length as the precision: \"%%.*v\"\n");
    fflush(stdout);

    vslprintf_v(rhostname, sizeof(rhostname), ruserlen, ruser);

    printf("formatted without reading past the field\n");
    free(ruser);
    return 0;
}
