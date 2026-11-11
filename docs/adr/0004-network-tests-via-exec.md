# ADR 4: test the network with `docker exec` and real tools

**Status:** accepted

**Context.** A rendered config that parses is not a working network. Mocking the data plane
would test the mock.

**Decision.** pytest runs `ping`, `traceroute -I`, `curl`, `ip -j route`, `vtysh … json`,
`nft -j` and `dig` inside the lab containers and asserts on their output. Expected values come
from the IPAM API where possible (e.g. every DNS name in IPAM must resolve to its IPAM
address). Negative tests prove the firewall, not routing, blocks traffic: the outside host has
a route to the LAN, and the blocked service is reachable from the border itself.

**Consequences.** Tests need the lab running (`make demo`), so they carry a `lab` marker and run
in a separate CI job. The deployer waits for full OSPF adjacencies and a stable routing table
before tests start, otherwise asymmetric paths during convergence cause flaky failures.
