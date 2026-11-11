from ipaddress import ip_address, ip_network

import pytest

from app.ipam import next_free, usable_count


@pytest.mark.parametrize(
    ("prefix", "count"),
    [
        ("10.0.0.0/24", 254),
        ("10.0.0.0/29", 6),
        ("10.0.0.0/30", 2),
        ("10.0.0.0/31", 2),  # RFC 3021 point-to-point
        ("10.0.0.1/32", 1),
        ("2001:db8::/126", 3),  # minus Subnet-Router anycast
        ("2001:db8::/127", 2),  # RFC 6164
        ("2001:db8::/64", 2**64 - 1),
    ],
)
def test_usable_count(prefix: str, count: int) -> None:
    net = ip_network(prefix)
    assert usable_count(net) == count
    if net.num_addresses <= 256:
        assert len(list(net.hosts())) == count


def test_next_free_skips_network_and_used() -> None:
    net = ip_network("192.168.0.0/29")
    assert next_free(net, []) == ip_address("192.168.0.1")
    assert next_free(net, [ip_address("192.168.0.1")]) == ip_address("192.168.0.2")


def test_next_free_exhausted() -> None:
    net = ip_network("192.168.0.0/30")
    assert next_free(net, [ip_address("192.168.0.1"), ip_address("192.168.0.2")]) is None


def test_next_free_ipv6_large_prefix_is_lazy() -> None:
    net = ip_network("2001:db8:1::/64")
    used = [ip_address("2001:db8:1::1"), ip_address("2001:db8:1::2")]
    assert next_free(net, used) == ip_address("2001:db8:1::3")
