/* Reachability of the def.c:112 over-read from lwIP's HTTP server.
 *
 * http_parse_request(), src/apps/http/httpd.c:2066:
 *     left_len = (u16_t)(data_len - ((sp1 + 1) - data));
 *     sp2 = lwip_strnstr(sp1 + 1, " ", left_len);
 *
 * The defect in lwip_strnstr reads buffer[n] and is reachable ONLY with a
 * one-character token, because with tokenlen >= 2 the bound fails an
 * iteration earlier. Every other call site in the tree passes CRLF,
 * CRLF CRLF, or a header name. This one passes " ".
 *
 * The scan runs to the bound whenever the URI region holds no space and no
 * NUL -- i.e. any request line with no space after the URI.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* lwip_strnstr, verbatim from src/core/def.c:105 */
char *lwip_strnstr(const char *buffer, const char *token, size_t n)
{
  const char *p;
  size_t tokenlen = strlen(token);
  if (tokenlen == 0) {
    return (char *)buffer;
  }
  for (p = buffer; *p && (p + tokenlen <= buffer + n); p++) {
    if ((*p == *token) && (strncmp(p, token, tokenlen) == 0)) {
      return (char *)p;
    }
  }
  return NULL;
}

int main(void)
{
    /* One pbuf holding exactly the request, as http_parse_request sees it
     * when p->len == p->tot_len:  data = p->payload; data_len = p->len; */
    const char *req = "GET /aaaaaaaa\r\n\r\n";     /* no space after the URI */
    size_t data_len = strlen(req);
    char *data = (char *)malloc(data_len);         /* exactly len, no NUL */
    memcpy(data, req, data_len);

    char *sp1 = data + 3;                          /* the space after GET */
    unsigned short left_len = (unsigned short)(data_len - ((sp1 + 1) - data));

    printf("request %zu bytes, searching %u bytes for a single space\n",
           data_len, left_len);
    fflush(stdout);

    char *sp2 = lwip_strnstr(sp1 + 1, " ", left_len);

    printf("returned %p\n", (void *)sp2);
    free(data);
    return 0;
}
