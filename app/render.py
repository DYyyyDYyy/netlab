"""Render device configs (FRR, nftables, CoreDNS) from IPAM data via Jinja2."""

import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.ipam import Invalid, NotFound, get_device
from app.models import Address, Device, DeviceKind, IPAddress, Subnet, SubnetRole
from app.policy import DEFAULT_POLICY, FirewallPolicy

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"

_env = Environment(
    loader=FileSystemLoader(TEMPLATES),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)

PASSIVE_ROLES = {SubnetRole.lan, SubnetRole.dmz, SubnetRole.loopback}


@dataclass(frozen=True)
class Iface:
    name: str
    ip: IPAddress
    subnet: Subnet

    @property
    def cidr(self) -> str:
        return f"{self.ip}/{self.subnet.prefix.prefixlen}"


def _ifaces(device: Device) -> list[Iface]:
    out = [Iface(a.interface or "", a.ip, a.subnet) for a in device.addresses]
    return sorted(out, key=lambda i: (i.subnet.role != SubnetRole.loopback, i.name))


def _router(session: Session, name: str) -> Device:
    device = get_device(session, name)
    if device.kind != DeviceKind.router:
        raise Invalid(f"{name} is a {device.kind}, not a router")
    return device


def render_frr(session: Session, name: str) -> str:
    device = _router(session, name)
    ifaces = _ifaces(device)
    loopbacks = [i for i in ifaces if i.subnet.role == SubnetRole.loopback]
    if not loopbacks:
        raise Invalid(f"{name} has no loopback address (router-id)")
    wan = [i for i in ifaces if i.subnet.role == SubnetRole.wan]
    ctx: dict[str, Any] = {
        "hostname": device.name,
        "router_id": loopbacks[0].ip,
        "loopbacks": loopbacks,
        "transit": [i for i in ifaces if i.subnet.role == SubnetRole.transit],
        "ospf": [i for i in ifaces if i.subnet.role != SubnetRole.wan],
        "passive": [i for i in ifaces if i.subnet.role in PASSIVE_ROLES and i.name != "lo"],
        "upstream": wan[0].subnet.gateway if wan else None,
    }
    return _env.get_template("frr.conf.j2").render(ctx)


def render_daemons() -> str:
    return _env.get_template("daemons.j2").render()


def _resolve(session: Session, dns_name: str) -> IPAddress:
    ip = session.scalar(select(Address.ip).where(Address.dns_name == dns_name))
    if ip is None:
        raise NotFound(f"no IPAM address with dns_name {dns_name!r}")
    return ip


def render_nftables(session: Session, name: str, policy: FirewallPolicy = DEFAULT_POLICY) -> str:
    device = _router(session, name)
    wan = [i for i in _ifaces(device) if i.subnet.role == SubnetRole.wan]
    if not wan:
        raise Invalid(f"{name} has no wan interface, it is not a border router")
    inside = session.scalars(
        select(Subnet.prefix)
        .where(Subnet.vrf_id == wan[0].subnet.vrf_id, Subnet.role != SubnetRole.wan)
        .where(func.family(Subnet.prefix) == 4)  # NAT44 only
        .order_by(Subnet.prefix)
    ).all()
    forwards = [(pf, _resolve(session, pf.target)) for pf in policy.port_forwards]
    ctx: dict[str, Any] = {
        "hostname": device.name,
        "wan": wan[0],
        "inside": inside,
        "policy": policy,
        "forwards": forwards,
    }
    return _env.get_template("nftables.conf.j2").render(ctx)


def _records(session: Session) -> list[Address]:
    stmt = (
        select(Address)
        .options(selectinload(Address.subnet))
        .where(Address.dns_name.is_not(None))
        .order_by(Address.dns_name, Address.ip)
    )
    return list(session.scalars(stmt))


def _serial(text: str) -> int:
    """Deterministic SOA serial that changes whenever the zone content changes."""
    return zlib.crc32(text.encode()) % 2_000_000_000 + 1


def render_zone(session: Session, zone: str) -> str:
    records = [(a.dns_name, "AAAA" if a.ip.version == 6 else "A", a.ip) for a in _records(session)]
    body = "".join(f"{n}{t}{ip}" for n, t, ip in records)
    return _env.get_template("zone.j2").render(
        zone=zone, serial=_serial(body), records=records, ns_ip=_resolve(session, "dns1")
    )


def render_reverse_zone(session: Session, zone: str) -> str:
    ptrs = [
        (a.ip.reverse_pointer, f"{a.dns_name}.{zone}.")
        for a in _records(session)
        if a.ip.version == 4
    ]
    body = "".join(f"{p}{t}" for p, t in ptrs)
    return _env.get_template("reverse.zone.j2").render(zone=zone, serial=_serial(body), ptrs=ptrs)


def render_corefile(zone: str) -> str:
    return _env.get_template("Corefile.j2").render(zone=zone)


def topology(session: Session) -> dict[str, Any]:
    """Physical view of IPAM for the lab deployer: docker networks and attachments."""
    subnets = session.scalars(
        select(Subnet).options(selectinload(Subnet.addresses)).order_by(Subnet.name)
    ).all()
    devices = session.scalars(
        select(Device).options(selectinload(Device.addresses)).order_by(Device.name)
    ).all()
    networks = [
        {
            "name": s.name,
            "prefix": str(s.prefix),
            "role": s.role.value,
            "gateway": str(s.gateway) if s.gateway else None,
            "reserved": [
                {"ip": str(a.ip), "description": a.description}
                for a in s.addresses
                if a.device_id is None
            ],
        }
        for s in subnets
        if s.role != SubnetRole.loopback
    ]
    inside = [s for s in subnets if s.role != SubnetRole.wan]
    out_devices = []
    for d in devices:
        ifaces = [i for i in _ifaces(d) if i.subnet.role != SubnetRole.loopback]
        routes: list[dict[str, str]] = []
        if d.kind in (DeviceKind.host, DeviceKind.server, DeviceKind.dns) and ifaces:
            gw = ifaces[0].subnet.gateway
            if gw is not None:
                routes.append({"dst": "default", "via": str(gw)})
        if d.kind == DeviceKind.external and ifaces:
            border = next(
                (
                    a
                    for a in ifaces[0].subnet.addresses
                    if a.device and a.device.kind == DeviceKind.router
                ),
                None,
            )
            if border is not None:
                routes += [{"dst": str(s.prefix), "via": str(border.ip)} for s in inside]
        out_devices.append(
            {
                "name": d.name,
                "kind": d.kind.value,
                "interfaces": [
                    {"name": i.name, "network": i.subnet.name, "ip": str(i.ip)} for i in ifaces
                ],
                "routes": routes,
            }
        )
    return {"networks": networks, "devices": out_devices}
