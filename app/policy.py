"""Firewall/NAT policy for border routers, kept as code next to the IPAM data."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PortForward:
    proto: str
    external_port: int
    target: str  # dns_name of an IPAM address
    target_port: int


@dataclass(frozen=True)
class FirewallPolicy:
    # inside -> internet: everything not listed here is dropped (default deny)
    allow_out_tcp: tuple[int, ...] = (80, 443)
    allow_out_udp: tuple[int, ...] = (53, 123)
    allow_out_icmp: bool = True
    # internet -> inside: only explicit DNAT port forwards
    port_forwards: tuple[PortForward, ...] = field(default=(PortForward("tcp", 8080, "srv1", 80),))


DEFAULT_POLICY = FirewallPolicy()
