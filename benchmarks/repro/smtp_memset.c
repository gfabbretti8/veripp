/* lwIP smtp_set_auth(): a hardcoded 64 against a configurable buffer.
 *
 * src/apps/smtp/smtp.c:291
 *   static char smtp_auth_plain[SMTP_MAX_USERNAME_LEN + SMTP_MAX_PASS_LEN + 3];
 *
 * src/apps/smtp/smtp.c:~407, first statement of smtp_set_auth()
 *   memset(smtp_auth_plain, 0xfa, 64);
 *
 * SMTP_MAX_USERNAME_LEN and SMTP_MAX_PASS_LEN are documented options in
 * smtp_opts.h, defaulting to 32 each -- so the buffer is 67 bytes and the
 * memset fits with three to spare. Lowering them to save RAM, which is the
 * whole reason they are options on an embedded stack, overflows a static
 * buffer with a fixed 0xfa pattern.
 */
#include <stdio.h>
#include <string.h>

#ifndef SMTP_MAX_USERNAME_LEN
#define SMTP_MAX_USERNAME_LEN   16      /* tuned down from the default 32 */
#endif
#ifndef SMTP_MAX_PASS_LEN
#define SMTP_MAX_PASS_LEN       16      /* tuned down from the default 32 */
#endif

static char smtp_auth_plain[SMTP_MAX_USERNAME_LEN + SMTP_MAX_PASS_LEN + 3];
static char canary[32] = "canary-must-not-be-touched";

int main(void)
{
    printf("SMTP_MAX_USERNAME_LEN=%d SMTP_MAX_PASS_LEN=%d\n",
           SMTP_MAX_USERNAME_LEN, SMTP_MAX_PASS_LEN);
    printf("sizeof(smtp_auth_plain) = %zu, memset writes 64\n",
           sizeof(smtp_auth_plain));
    fflush(stdout);

    memset(smtp_auth_plain, 0xfa, 64);      /* verbatim from smtp_set_auth */

    printf("canary: %s\n", canary);
    return 0;
}
