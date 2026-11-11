# ADR 3: generate device configs from IPAM

**Status:** accepted

**Context.** Hand-written router, firewall and DNS configs drift from the address plan: an IP
changes in one place and not in the others.

**Decision.** IPAM is the source of truth. Jinja2 templates (`StrictUndefined`) render
`frr.conf`, `nftables.conf`, the forward and reverse zones and the `Corefile` from the database.
Roles on subnets (`lan`, `dmz`, `transit`, `loopback`, `wan`) drive behaviour: OSPF networks,
passive interfaces, which prefixes get NAT. The firewall policy is a small typed object in
code. The lab deployer only fetches `/render/*` output; it contains no addresses itself.

**Consequences.** Snapshot tests pin the rendered output, so a template or data change shows
up as a reviewable diff. The DNS serial is a hash of the records, so it changes only when the
zone does.
