FROM quay.io/frrouting/frr:10.2.1
RUN apk add --no-cache nftables tcpdump curl
COPY --chmod=755 entrypoint.sh /usr/local/bin/netlab-entrypoint
ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/netlab-entrypoint"]
CMD ["/usr/lib/frr/docker-start"]
