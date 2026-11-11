# ADR 2: plain Docker instead of containerlab

**Status:** accepted

**Context.** containerlab is the usual tool for container network labs, but it needs root
(or sudo) to create veth links and network namespaces, and is another dependency in CI.

**Decision.** Every IPAM subnet becomes a Docker bridge network, every device a container
attached with a fixed IP and interface name (`com.docker.network.endpoint.ifname`). Routers
get only `NET_ADMIN`, `NET_RAW`, `SYS_ADMIN` and namespaced sysctls — no `--privileged`, no
host networking. Lab networks use `gateway_mode_ipv4=nat-unprotected` because Docker >= 28
otherwise drops traffic routed between bridges by the lab routers.

**Consequences.** Runs rootless-friendly on any Docker host and on GitHub-hosted runners. Links
are bridges, not point-to-point veths, so the Docker host holds one address per segment (IPAM
reserves it) and link failure is simulated with `ip link set down` inside a router.
