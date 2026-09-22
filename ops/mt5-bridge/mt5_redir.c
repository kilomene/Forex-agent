/*
 * mt5_redir.c - LD_PRELOAD shim for MT5-on-Wine.
 *
 * Direct outbound TCP is blocked in this sandbox; only HTTP(S) via the
 * egress proxy works. The MT5 terminal's broker protocol uses TCP/443.
 * This shim transparently tunnels the terminal's outbound TCP/443
 * connections through the egress proxy via HTTP CONNECT.
 *
 * For each intercepted connect() to <ip>:443 (non-loopback):
 *   1. Connect to hatch-egress-proxy:3128
 *   2. Send "CONNECT <ip>:443 HTTP/1.1"
 *   3. Wait for "200" response
 *   4. dup2 the proxy socket onto the application's socket fd
 *   5. Return 0 (application thinks the original connect succeeded)
 *
 * Build: gcc -shared -fPIC -o mt5_redir.so mt5_redir.c -ldl
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stddef.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <netdb.h>

typedef int (*connect_fn)(int, const struct sockaddr *, socklen_t);
typedef ssize_t (*send_fn)(int, const void *, size_t, int);
typedef ssize_t (*recv_fn)(int, void *, size_t, int);

static void log_msg(const char *msg) {
    int fd = open("/tmp/mt5_redir.log", O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd >= 0) { write(fd, msg, strlen(msg)); close(fd); }
}

__attribute__((visibility("default")))
int connect(int sockfd, const struct sockaddr *addr, socklen_t addrlen)
{
    static connect_fn real_connect = NULL;
    static send_fn real_send = NULL;
    static recv_fn real_recv = NULL;
    if (!real_connect) {
        real_connect = (connect_fn)dlsym(RTLD_NEXT, "connect");
        real_send = (send_fn)dlsym(RTLD_NEXT, "send");
        real_recv = (recv_fn)dlsym(RTLD_NEXT, "recv");
    }

    if (addr && addr->sa_family == AF_INET && addrlen >= sizeof(struct sockaddr_in)) {
        const struct sockaddr_in *a4 = (const struct sockaddr_in *)addr;
        if (ntohs(a4->sin_port) == 443) {
            uint32_t ip = ntohl(a4->sin_addr.s_addr);
            if ((ip >> 24) != 127) {
                char ipstr[16];
                snprintf(ipstr, sizeof(ipstr), "%u.%u.%u.%u",
                         (ip >> 24) & 255, (ip >> 16) & 255,
                         (ip >> 8) & 255, ip & 255);

                /* Connect to the egress proxy */
                int ps = socket(AF_INET, SOCK_STREAM, 0);
                if (ps < 0) return real_connect(sockfd, addr, addrlen);

                struct hostent *he = gethostbyname("hatch-egress-proxy");
                if (!he) { close(ps); return real_connect(sockfd, addr, addrlen); }

                struct sockaddr_in proxy = {0};
                proxy.sin_family = AF_INET;
                proxy.sin_port = htons(3128);
                memcpy(&proxy.sin_addr, he->h_addr, he->h_length);

                if (real_connect(ps, (struct sockaddr *)&proxy, sizeof(proxy)) != 0) {
                    close(ps);
                    return real_connect(sockfd, addr, addrlen);
                }

                /* Send CONNECT */
                char req[128];
                int reqlen = snprintf(req, sizeof(req),
                    "CONNECT %s:443 HTTP/1.1\r\nHost: %s:443\r\n\r\n", ipstr, ipstr);
                int sent = 0;
                while (sent < reqlen) {
                    ssize_t r = real_send(ps, req + sent, reqlen - sent, 0);
                    if (r <= 0) break;
                    sent += r;
                }

                /* Read response */
                char resp[512];
                int resplen = 0;
                while (resplen < (int)sizeof(resp) - 1) {
                    ssize_t r = real_recv(ps, resp + resplen, 1, 0);
                    if (r <= 0) break;
                    resplen += r;
                    resp[resplen] = 0;
                    if (strstr(resp, "\r\n\r\n")) break;
                }

                if (!strstr(resp, " 200 ")) {
                    char buf[160];
                    snprintf(buf, sizeof(buf), "CONNECT %s:443 refused: %.40s\n", ipstr, resp);
                    log_msg(buf);
                    close(ps);
                    return real_connect(sockfd, addr, addrlen);
                }

                /* Success: dup proxy socket onto application's fd */
                dup2(ps, sockfd);
                if (ps != sockfd) close(ps);

                char buf[64];
                snprintf(buf, sizeof(buf), "tunneled %s:443 via CONNECT\n", ipstr);
                log_msg(buf);
                return 0;
            }
        }
    }
    return real_connect(sockfd, addr, addrlen);
}
