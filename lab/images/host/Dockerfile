FROM coredns/coredns:1.11.3 AS coredns

FROM alpine:3.20
RUN apk add --no-cache bind-tools iputils iproute2 traceroute curl busybox-extras tcpdump
COPY --from=coredns /coredns /usr/local/bin/coredns
COPY --chmod=755 whoami.cgi /www/cgi-bin/whoami
COPY --chmod=755 entrypoint.sh /usr/local/bin/netlab-entrypoint
ENTRYPOINT ["/usr/local/bin/netlab-entrypoint"]
CMD ["sleep", "infinity"]
