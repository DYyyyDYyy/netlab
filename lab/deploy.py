"""Build the docker lab from IPAM: every subnet becomes a docker network, every device a
container, and every config file is rendered by the API (nothing is hand-written).

    uv run python -m lab.deploy            # (re)create the lab
    uv run python -m lab.deploy --destroy  # remove it
"""

import argparse
import ipaddress
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

LABEL = "netlab.lab=1"
BUILD = Path(__file__).resolve().parent / "build"
ROUTER_CAPS = ["NET_ADMIN", "NET_RAW", "SYS_ADMIN"]
ROUTER_SYSCTLS = [
    "net.ipv4.ip_forward=1",
    "net.ipv4.conf.all.rp_filter=0",
    "net.ipv4.conf.default.rp_filter=0",
]


def docker(*args: str, check: bool = True) -> str:
    res = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)}\n{res.stderr.strip()}")
    return res.stdout.strip()


def cname(device: str) -> str:
    return f"netlab-{device}"


def destroy() -> None:
    ids = docker("ps", "-aq", "--filter", f"label={LABEL}").split()
    if ids:
        docker("rm", "-f", *ids)
    nets = docker("network", "ls", "-q", "--filter", f"label={LABEL}").split()
    if nets:
        docker("network", "rm", *nets)
    print(f"removed {len(ids)} containers, {len(nets)} networks")


def create_networks(topo: dict[str, Any]) -> None:
    for net in topo["networks"]:
        bridge_gw = next(
            r["ip"] for r in net["reserved"] if r["description"] == "docker bridge gateway"
        )
        docker(
            "network", "create", "--driver", "bridge", "--label", LABEL,
            "--subnet", net["prefix"], "--gateway", bridge_gw,
            "-o", f"com.docker.network.bridge.name=nl-{net['name']}"[:47],
            "-o", "com.docker.network.bridge.enable_ip_masquerade=false",
            # Docker >= 28 drops packets to a container IP arriving from another bridge
            # ("direct routing" protection); with br_netfilter that also kills our transit
            # traffic, which is routed by the lab routers, not by the host.
            "-o", "com.docker.network.bridge.gateway_mode_ipv4=nat-unprotected",
            f"netlab-{net['name']}",
        )  # fmt: skip


def render_files(api: httpx.Client, dev: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Return (mount point, {filename: content}) for a device, all rendered by the API."""
    name, kind = dev["name"], dev["kind"]

    def get(url: str) -> str:
        r = api.get(url)
        r.raise_for_status()
        return r.text

    if kind == "router":
        files = {
            "frr.conf": get(f"/render/frr/{name}"),
            "daemons": get("/render/frr-daemons"),
            "vtysh.conf": "service integrated-vtysh-config\n",
        }
        if any(i["network"] == "wan" for i in dev["interfaces"]):
            files["nftables.conf"] = get(f"/render/nftables/{name}")
        return "/etc/frr", files
    if kind == "dns":
        zone = "netlab.internal"
        return "/etc/coredns", {
            "Corefile": get("/render/coredns/Corefile"),
            f"db.{zone}": get("/render/coredns/zone"),
            "db.in-addr.arpa": get("/render/coredns/reverse"),
        }
    return "", {}


def create_device(dev: dict[str, Any], mount: str, files: dict[str, str]) -> None:
    name, kind = dev["name"], dev["kind"]
    args = ["create", "--name", cname(name), "--hostname", name, "--label", LABEL]
    for i in dev["interfaces"]:
        args += [
            "--network",
            f"name=netlab-{i['network']},ip={i['ip']},"
            f"driver-opt=com.docker.network.endpoint.ifname={i['name']}",
        ]
    if kind == "router":
        args += [f"--cap-add={c}" for c in ROUTER_CAPS]
        args += [f"--sysctl={s}" for s in ROUTER_SYSCTLS]
        args += ["netlab-router"]
    else:
        routes = ";".join(f"{r['dst']} via {r['via']}" for r in dev["routes"])
        serve = {"server": "80", "external": "80 22"}.get(kind, "")
        args += ["--cap-add=NET_ADMIN", "-e", f"ROUTES={routes}", "-e", f"SERVE_HTTP={serve}"]
        args += ["netlab-host"]
        if kind == "dns":
            args += ["coredns", "-conf", "/etc/coredns/Corefile"]
    docker(*args)

    out = BUILD / name
    out.mkdir(parents=True, exist_ok=True)
    for fname, content in files.items():
        (out / fname).write_text(content)
    if files:
        docker("cp", f"{out}/.", f"{cname(name)}:{mount}")
    docker("start", cname(name))


def routes_of(device: str) -> dict[str, str]:
    raw = docker("exec", cname(device), "ip", "-j", "route", check=False)
    return {r["dst"]: r.get("gateway", r.get("dev", "")) for r in json.loads(raw or "[]")}


def full_neighbors(device: str) -> int:
    raw = docker("exec", cname(device), "vtysh", "-c", "show ip ospf neighbor json", check=False)
    try:
        nbrs = json.loads(raw)["neighbors"].values()
    except (ValueError, KeyError):
        return 0
    return sum(n["nbrState"].startswith("Full") for group in nbrs for n in group)


def wait_converged(topo: dict[str, Any], timeout: float = 90) -> float:
    """Converged = every transit adjacency is Full, every router has every inside prefix plus
    a default route, and the routing tables did not change between two polls.

    Merely having the prefixes is not enough: while r1-r2 is still coming up, return traffic
    can take a path through the border, whose stateful firewall drops the half-seen flow.
    """
    roles = {n["name"]: n["role"] for n in topo["networks"]}
    inside = {n["prefix"] for n in topo["networks"] if n["role"] != "wan"} | {"default"}
    routers = {
        d["name"]: sum(roles[i["network"]] == "transit" for i in d["interfaces"])
        for d in topo["devices"]
        if d["kind"] == "router"
    }
    start, previous = time.monotonic(), None
    while True:
        tables = {r: routes_of(r) for r in routers}
        missing = {r: inside - t.keys() for r, t in tables.items() if inside - t.keys()}
        down = {r: n for r, n in routers.items() if full_neighbors(r) < n}
        if not missing and not down and tables == previous:
            return time.monotonic() - start
        if time.monotonic() - start > timeout:
            raise RuntimeError(f"OSPF did not converge: missing={missing} adjacencies={down}")
        previous = tables
        time.sleep(1)


def deploy(api_url: str) -> None:
    t0 = time.monotonic()
    with httpx.Client(base_url=api_url, timeout=10) as api:
        topo = api.get("/topology").raise_for_status().json()
        destroy()
        create_networks(topo)
        for dev in topo["devices"]:
            mount, files = render_files(api, dev)
            create_device(dev, mount, files)
    took = wait_converged(topo)
    print(f"OSPF converged in {took:.1f}s, lab up in {time.monotonic() - t0:.1f}s")
    print(f"rendered configs: {BUILD}")
    for dev in topo["devices"]:
        ips = ", ".join(f"{i['name']}={i['ip']}" for i in dev["interfaces"])
        print(f"  {cname(dev['name']):<15} {dev['kind']:<9} {ips}")
    lan_host = ipaddress.ip_address(
        next(d for d in topo["devices"] if d["name"] == "host1")["interfaces"][0]["ip"]
    )
    print(
        "\ntry:\n"
        "  docker exec netlab-host1 traceroute -n 203.0.113.100\n"
        "  docker exec netlab-host1 curl -s 203.0.113.100/cgi-bin/whoami   # NAT -> 203.0.113.1\n"
        "  docker exec netlab-host1 dig +short @10.10.2.53 srv1.netlab.internal\n"
        f"  docker exec netlab-host1 dig +short @10.10.2.53 -x {lan_host}\n"
        "  docker exec netlab-isp curl -s 203.0.113.1:8080                 # DNAT -> srv1\n"
        "  docker exec netlab-border nft list ruleset\n"
        "  docker exec netlab-r1 vtysh -c 'show ip ospf neighbor'"
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--destroy", action="store_true")
    p.add_argument("--api", default=os.environ.get("NETLAB_API", "http://127.0.0.1:8000"))
    a = p.parse_args()
    try:
        destroy() if a.destroy else deploy(a.api)
    except (RuntimeError, httpx.HTTPError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
