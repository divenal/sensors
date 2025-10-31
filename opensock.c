/* Opens a socket receiving raw ethernet frames from zappi, sent to
 * multicast address 71:b3:d5:3a:6f:00 protocol 0x88b5  (reserved for experiments)
 * It must be "installed  with
 *   setcap cap_net_raw=pe
 * to give it permission to open the socket. (Or setuid root, I suppose.)
 */


#include <stdio.h>
#include <unistd.h>
#include <sys/socket.h>
#include <netpacket/packet.h>
#include <arpa/inet.h>
#include <net/ethernet.h>
#include <net/if.h>

int main(int argc, char *argv[])
{
    if (argc < 3)
    {
        fprintf(stderr, "Usage: %s <interface> prog args ...\n", argv[0]);
        return 1;
    }
    
    int sock = socket(AF_PACKET, SOCK_RAW, htons(0x88b5));
    if (sock < 0)
    {
        perror("cannot open socket");
        return 1;
    }

    unsigned int ifidx = if_nametoindex(argv[1]);
    if (ifidx == 0)
    {
        perror("cannot find interface");
        return 1;
    }

    struct packet_mreq multi = {
        ifidx,
        PACKET_MR_MULTICAST,
        6,
        { 0x71, 0xb3, 0xd5, 0x3a, 0x6f, 0x00 }
    };

    int ss = setsockopt(sock, SOL_PACKET, PACKET_ADD_MEMBERSHIP, &multi, sizeof(multi));
    if (ss < 0)
    {
        perror("cannot setsockopt");
        return 1;
    }

    dup2(sock, 42);
    close(sock);
    execvp(argv[2], argv + 2);

    perror("exec failed");
    return 1;
}
