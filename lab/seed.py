"""Populate IPAM with the lab topology through the public API (idempotent).

host1,host2 --lan-- r1 ---r1-r2--- r2 --dmz-- srv1, dns1
                     \\            /
                 r1-border   r2-border
                       \\      /
                        border --wan-- isp
"""

import ipaddress
import os
import sys
from typing import Any

import httpx

Client = httpx.Client  # fastapi.testclient.TestClient is a subclass, so tests reuse seed()

VLANS = [(10, "users"), (20, "servers")]

DEVICES = {
    "r1": "router",
    "r2": "router",
    "border": "router",
    "host1": "host",
    "host2": "host",
    "srv1": "server",
    "dns1": "dns",
    "isp": "external",
}

# name, prefix, role, vlan, gateway
SUBNETS: list[tuple[str, str, str, int | None, str | None]] = [
    ("loopbacks", "10.255.0.0/24", "loopback", None, None),
    ("lan", "10.10.1.0/24", "lan", 10, "10.10.1.1"),
    ("dmz", "10.10.2.0/24", "dmz", 20, "10.10.2.1"),
    ("r1-r2", "10.0.12.0/29", "transit", None, None),
    ("r1-border", "10.0.13.0/29", "transit", None, None),
    ("r2-border", "10.0.23.0/29", "transit", None, None),
    ("wan", "203.0.113.0/24", "wan", None, "203.0.113.100"),
]

# subnet, device, interface, ip (None = allocate next free), dns_name
ADDRESSES: list[tuple[str, str, str, str | None, str | None]] = [
    ("loopbacks", "r1", "lo", "10.255.0.1", "r1"),
    ("loopbacks", "r2", "lo", "10.255.0.2", "r2"),
    ("loopbacks", "border", "lo", "10.255.0.3", "border"),
    ("lan", "r1", "lan0", "10.10.1.1", "r1-lan0"),
    ("dmz", "r2", "dmz0", "10.10.2.1", "r2-dmz0"),
    ("dmz", "dns1", "eth0", "10.10.2.53", "dns1"),
    ("r1-r2", "r1", "c12", "10.0.12.1", "r1-c12"),
    ("r1-r2", "r2", "c12", "10.0.12.2", "r2-c12"),
    ("r1-border", "r1", "c13", "10.0.13.1", "r1-c13"),
    ("r1-border", "border", "c13", "10.0.13.2", "border-c13"),
    ("r2-border", "r2", "c23", "10.0.23.1", "r2-c23"),
    ("r2-border", "border", "c23", "10.0.23.2", "border-c23"),
    ("wan", "border", "wan0", "203.0.113.1", "border-wan0"),
    ("wan", "isp", "eth0", "203.0.113.100", None),
    # end hosts get the next free address from IPAM
    ("lan", "host1", "eth0", None, "host1"),
    ("lan", "host2", "eth0", None, "host2"),
    ("dmz", "srv1", "eth0", None, "srv1"),
]


def _ok(resp: httpx.Response) -> None:
    if resp.status_code not in (200, 201, 409):
        req = resp.request
        raise RuntimeError(f"{req.method} {req.url}: {resp.status_code} {resp.text}")


def seed(client: Client) -> None:
    _ok(client.post("/vrfs", json={"name": "default", "rd": "65000:1"}))
    for vid, name in VLANS:
        _ok(client.post("/vlans", json={"vid": vid, "name": name}))
    for name, kind in DEVICES.items():
        _ok(client.post("/devices", json={"name": name, "kind": kind}))
    for name, prefix, role, vlan, gw in SUBNETS:
        _ok(
            client.post(
                "/subnets",
                json={"name": name, "prefix": prefix, "role": role, "vlan": vlan, "gateway": gw},
            )
        )
        net = ipaddress.ip_network(prefix)
        if role != "loopback":  # docker needs one address per bridge for itself
            bridge = str(net.broadcast_address - 1)
            _ok(
                client.post(
                    f"/subnets/{name}/addresses",
                    json={"ip": bridge, "description": "docker bridge gateway"},
                )
            )

    existing = {(a["subnet"], a["device"]) for a in client.get("/addresses").json()}
    for subnet, device, iface, ip, dns in ADDRESSES:
        if (subnet, device) in existing:
            continue
        body: dict[str, Any] = {"device": device, "interface": iface, "dns_name": dns}
        if ip is None:
            _ok(client.post(f"/subnets/{subnet}/allocate", json=body))
        else:
            _ok(client.post(f"/subnets/{subnet}/addresses", json={**body, "ip": ip}))


def main() -> None:
    base = (
        sys.argv[1] if len(sys.argv) > 1 else os.environ.get("NETLAB_API", "http://localhost:8000")
    )
    with httpx.Client(base_url=base, timeout=10) as client:
        seed(client)
        for s in client.get("/subnets").json():
            print(f"  {s['name']:<10} {s['prefix']:<16} used {s['used']}/{s['size']}")


if __name__ == "__main__":
    main()
