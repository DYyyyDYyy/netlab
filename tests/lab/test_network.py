"""End-to-end network tests against the running lab (`make demo`, then `make lab-test`).

Every check runs a real tool inside a container via `docker exec`: ip route, vtysh,
ping, traceroute, curl, nft, dig. Expected values come from the IPAM API, not literals,
wherever possible.
"""

import json
import os
import subprocess
import time

import httpx
import pytest

pytestmark = [
    pytest.mark.lab,
    pytest.mark.skipif(not os.environ.get("NETLAB_LAB"), reason="set NETLAB_LAB=1 (make lab-test)"),
]

API = os.environ.get("NETLAB_API", "http://127.0.0.1:8000")
ZONE = "netlab.internal"
WAN_IP = "203.0.113.1"
ISP = "203.0.113.100"
DNS = "10.10.2.53"


def sh(device: str, *cmd: str, timeout: float = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", f"netlab-{device}", *cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def ok(device: str, *cmd: str) -> str:
    res = sh(device, *cmd)
    assert res.returncode == 0, f"{device}: {' '.join(cmd)}\n{res.stdout}{res.stderr}"
    return res.stdout.strip()


@pytest.fixture(scope="module")
def ipam() -> dict[str, str]:
    """dns_name -> ip for every IPAM address that has a name."""
    addrs = httpx.get(f"{API}/addresses", timeout=10).raise_for_status().json()
    return {a["dns_name"]: a["ip"] for a in addrs if a["dns_name"]}


# --- L3: routing ------------------------------------------------------------


@pytest.mark.parametrize("router", ["r1", "r2"])
def test_ospf_neighbors_full(router: str) -> None:
    out = json.loads(ok(router, "vtysh", "-c", "show ip ospf neighbor json"))
    states = [n["nbrState"] for nbrs in out["neighbors"].values() for n in nbrs]
    assert len(states) == 2
    assert all(s.startswith("Full") for s in states), states


@pytest.mark.parametrize(
    ("router", "prefix"),
    [("r1", "10.10.2.0/24"), ("r2", "10.10.1.0/24"), ("r1", "default"), ("r2", "default")],
)
def test_route_learned_via_ospf(router: str, prefix: str) -> None:
    routes = json.loads(ok(router, "ip", "-j", "route", "show", prefix))
    assert routes, f"{router} has no route to {prefix}"
    assert routes[0]["protocol"] == "ospf"


def test_lan_to_dmz_ping(ipam: dict[str, str]) -> None:
    ok("host1", "ping", "-c", "2", "-W", "2", ipam["srv1"])


def test_traceroute_path_lan_to_dmz(ipam: dict[str, str]) -> None:
    out = ok("host1", "traceroute", "-I", "-n", "-w", "1", "-q", "1", ipam["srv1"])
    hops = [line.split()[1] for line in out.splitlines()[1:]]
    assert hops == [ipam["r1-lan0"], ipam["r2-c12"], ipam["srv1"]]


# --- NAT --------------------------------------------------------------------


@pytest.mark.parametrize("host", ["host1", "host2"])
def test_pat_hides_inside_addresses(host: str) -> None:
    """The outside server sees the border WAN address for every inside host (PAT)."""
    assert ok(host, "curl", "-sf", "-m", "5", f"http://{ISP}/cgi-bin/whoami") == WAN_IP


def test_dnat_publishes_srv1() -> None:
    assert ok("isp", "curl", "-sf", "-m", "5", f"http://{WAN_IP}:8080/") == "srv1"


def test_nat_rules_loaded() -> None:
    ruleset = ok("border", "nft", "list", "table", "ip", "nat")
    assert f"snat to {WAN_IP}" in ruleset
    assert "dnat to 10.10.2.2:80" in ruleset


# --- firewall ---------------------------------------------------------------


def test_outbound_http_allowed_ssh_blocked() -> None:
    # isp runs a service on tcp/22; the border itself reaches it (output is not filtered) ...
    assert ok("border", "curl", "-sf", "-m", "3", f"http://{ISP}:22/") == "isp"
    # ... but forwarding inside -> internet on tcp/22 is not in the policy: default deny
    assert sh("host1", "curl", "-sf", "-m", "3", f"http://{ISP}:22/").returncode != 0


def test_inbound_to_inside_blocked(ipam: dict[str, str]) -> None:
    # isp has a route to 10.10.1.0/24 via the border, so only the firewall can stop this
    assert ok("isp", "ip", "route", "get", ipam["host1"]).startswith(
        f"{ipam['host1']} via {WAN_IP}"
    )
    assert sh("isp", "ping", "-c", "2", "-W", "1", ipam["host1"]).returncode != 0


def test_default_deny_counter_increments(ipam: dict[str, str]) -> None:
    def dropped() -> int:
        rules = json.loads(ok("border", "nft", "-j", "list", "chain", "inet", "filter", "forward"))
        for item in rules["nftables"]:
            rule = item.get("rule")
            if rule and rule.get("comment") == "forward default deny":
                return int(next(e["counter"]["packets"] for e in rule["expr"] if "counter" in e))
        raise AssertionError("default deny rule not found")

    before = dropped()
    sh("isp", "ping", "-c", "1", "-W", "1", ipam["srv1"])
    assert dropped() > before


# --- DNS --------------------------------------------------------------------


def test_dns_resolves_every_ipam_name(ipam: dict[str, str]) -> None:
    for name, ip in ipam.items():
        assert ok("host1", "dig", "+short", f"@{DNS}", f"{name}.{ZONE}") == ip, name


def test_reverse_dns(ipam: dict[str, str]) -> None:
    assert ok("host1", "dig", "+short", f"@{DNS}", "-x", ipam["host1"]) == f"host1.{ZONE}."


def test_unknown_name_nxdomain() -> None:
    out = ok("host1", "dig", f"@{DNS}", f"nope.{ZONE}")
    assert "status: NXDOMAIN" in out


# --- resilience -------------------------------------------------------------


def test_ospf_reroutes_around_failed_link(ipam: dict[str, str]) -> None:
    """Cut r1<->border: traffic from the LAN to the internet must fail over via r2."""
    trace = ["traceroute", "-I", "-n", "-w", "1", "-q", "1", ISP]
    assert ok("host1", *trace).splitlines()[2].split()[1] == ipam["border-c13"]
    ok("r1", "ip", "link", "set", "c13", "down")
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if sh("host1", "ping", "-c", "1", "-W", "1", ISP).returncode == 0:
                break
        hops = [line.split()[1] for line in ok("host1", *trace).splitlines()[1:]]
        assert hops == [ipam["r1-lan0"], ipam["r2-c12"], ipam["border-c23"], ISP]
    finally:
        ok("r1", "ip", "link", "set", "c13", "up")
