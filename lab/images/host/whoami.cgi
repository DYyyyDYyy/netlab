#!/bin/sh
# Echo the client address as seen by this server: proves source NAT from the outside.
# busybox httpd listens on a dual-stack socket and reports "[::ffff:a.b.c.d]".
addr=${REMOTE_ADDR#[}
addr=${addr%]}
addr=${addr#::ffff:}
printf 'Content-Type: text/plain\r\n\r\n%s\n' "$addr"
