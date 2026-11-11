#!/bin/sh
# End-host boot.
#   ROUTES="default via 10.10.1.1;10.10.0.0/16 via 203.0.113.1"  routes from IPAM (/topology)
#   SERVE_HTTP="80 22"                                         busybox httpd on these ports
set -eu
if [ -n "${ROUTES:-}" ]; then
    ip route del default 2>/dev/null || true
    echo "$ROUTES" | tr ';' '\n' | while read -r route; do
        [ -n "$route" ] && ip route replace $route
    done
fi
hostname > /www/index.html
for port in ${SERVE_HTTP:-}; do
    httpd -p "$port" -h /www
done
exec "$@"
