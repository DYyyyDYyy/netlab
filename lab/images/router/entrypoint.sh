#!/bin/sh
# Router boot: drop the docker bridge default route (routing comes from OSPF / FRR config),
# load the generated firewall if there is one, then hand over to FRR's watchfrr.
set -eu
ip route del default 2>/dev/null || true
if [ -s /etc/frr/nftables.conf ]; then
    nft -f /etc/frr/nftables.conf
fi
exec "$@"
