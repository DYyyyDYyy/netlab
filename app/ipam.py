"""IPAM business logic: subnet creation, overlap search, address allocation."""

import ipaddress
from collections.abc import Iterable, Iterator

from sqlalchemy import cast, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import schemas
from app.models import VLAN, VRF, Address, Device, IPAddress, IPNetwork, Subnet


class IPAMError(Exception):
    status_code = 400


class NotFound(IPAMError):
    status_code = 404


class Conflict(IPAMError):
    status_code = 409


class Invalid(IPAMError):
    status_code = 422


def usable_hosts(net: IPNetwork) -> Iterator[IPAddress]:
    """Assignable addresses: no network/broadcast for IPv4 (except /31 and /32, RFC 3021),
    no Subnet-Router anycast for IPv6 (handled by ``hosts()``)."""
    yield from net.hosts()


def usable_count(net: IPNetwork) -> int:
    if net.version == 4:
        return net.num_addresses if net.prefixlen >= 31 else net.num_addresses - 2
    return net.num_addresses if net.prefixlen >= 127 else net.num_addresses - 1


def next_free(net: IPNetwork, used: Iterable[IPAddress]) -> IPAddress | None:
    taken = set(used)
    for ip in usable_hosts(net):
        if ip not in taken:
            return ip
    return None


def _get_by_name[T: (VRF, Device, Subnet)](session: Session, model: type[T], name: str) -> T:
    obj = session.scalar(select(model).where(model.name == name))
    if obj is None:
        raise NotFound(f"{model.__tablename__} {name!r} not found")
    return obj


def get_vrf(session: Session, name: str) -> VRF:
    return _get_by_name(session, VRF, name)


def get_device(session: Session, name: str) -> Device:
    return _get_by_name(session, Device, name)


def get_subnet(session: Session, name: str) -> Subnet:
    return _get_by_name(session, Subnet, name)


def subnet_out(s: Subnet) -> schemas.SubnetOut:
    size = usable_count(s.prefix)
    used = len(s.addresses)
    return schemas.SubnetOut(
        id=s.id,
        name=s.name,
        vrf=s.vrf.name,
        prefix=s.prefix,
        role=s.role,
        vlan=s.vlan.vid if s.vlan else None,
        gateway=s.gateway,
        size=size,
        used=used,
        utilization=round(used / size, 4) if size else 1.0,
    )


def address_out(a: Address) -> schemas.AddressOut:
    return schemas.AddressOut(
        id=a.id,
        subnet=a.subnet.name,
        ip=a.ip,
        prefixlen=a.subnet.prefix.prefixlen,
        device=a.device.name if a.device else None,
        interface=a.interface,
        dns_name=a.dns_name,
        description=a.description,
    )


def list_subnets(session: Session) -> list[Subnet]:
    stmt = (
        select(Subnet)
        .options(selectinload(Subnet.addresses), selectinload(Subnet.vrf))
        .order_by(Subnet.vrf_id, Subnet.prefix)
    )
    return list(session.scalars(stmt))


def find_overlaps(session: Session, vrf: VRF, prefix: IPNetwork) -> list[Subnet]:
    stmt = (
        select(Subnet)
        .where(Subnet.vrf_id == vrf.id)
        .where(Subnet.prefix.op("&&")(cast(str(prefix), postgresql.CIDR)))
        .order_by(Subnet.prefix)
    )
    return list(session.scalars(stmt))


def create_subnet(session: Session, data: schemas.SubnetIn) -> Subnet:
    vrf = get_vrf(session, data.vrf)
    prefix = ipaddress.ip_network(data.prefix)
    if data.gateway is not None and data.gateway not in prefix:
        raise Invalid(f"gateway {data.gateway} is outside {prefix}")
    vlan = None
    if data.vlan is not None:
        vlan = session.scalar(select(VLAN).where(VLAN.vid == data.vlan))
        if vlan is None:
            raise NotFound(f"vlan {data.vlan} not found")
    if overlaps := find_overlaps(session, vrf, prefix):
        names = ", ".join(f"{s.name} ({s.prefix})" for s in overlaps)
        raise Conflict(f"{prefix} overlaps {names} in vrf {vrf.name}")
    subnet = Subnet(
        name=data.name, vrf=vrf, prefix=prefix, role=data.role, vlan=vlan, gateway=data.gateway
    )
    session.add(subnet)
    try:
        session.commit()
    except IntegrityError as exc:  # concurrent insert hit the EXCLUDE / UNIQUE constraint
        session.rollback()
        raise Conflict(str(exc.orig).splitlines()[0]) from exc
    return subnet


def _make_address(
    session: Session, subnet: Subnet, ip: IPAddress, data: schemas.AllocateIn
) -> Address:
    device = get_device(session, data.device) if data.device else None
    addr = Address(
        subnet=subnet,
        ip=ip,
        device=device,
        interface=data.interface,
        dns_name=data.dns_name,
        description=data.description,
    )
    session.add(addr)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise Conflict(f"{ip} is already assigned in {subnet.name}") from exc
    return addr


def _lock_subnet(session: Session, name: str) -> Subnet:
    """Row lock serialises concurrent allocations from the same subnet."""
    subnet = session.scalar(select(Subnet).where(Subnet.name == name).with_for_update())
    if subnet is None:
        raise NotFound(f"subnet {name!r} not found")
    return subnet


def allocate(session: Session, subnet_name: str, data: schemas.AllocateIn) -> Address:
    subnet = _lock_subnet(session, subnet_name)
    used: list[IPAddress] = [a.ip for a in subnet.addresses]
    if subnet.gateway is not None:
        used.append(subnet.gateway)
    ip = next_free(subnet.prefix, used)
    if ip is None:
        session.rollback()
        raise Conflict(f"subnet {subnet.name} ({subnet.prefix}) is exhausted")
    return _make_address(session, subnet, ip, data)


def assign(session: Session, subnet_name: str, data: schemas.AddressIn) -> Address:
    subnet = _lock_subnet(session, subnet_name)
    ip = ipaddress.ip_address(data.ip)
    if ip not in subnet.prefix:
        session.rollback()
        raise Invalid(f"{ip} is outside {subnet.prefix}")
    if subnet.prefix.num_addresses > 2 and ip in (
        subnet.prefix.network_address,
        subnet.prefix.broadcast_address if subnet.prefix.version == 4 else None,
    ):
        session.rollback()
        raise Invalid(f"{ip} is the network/broadcast address of {subnet.prefix}")
    return _make_address(session, subnet, ip, data)


def list_addresses(session: Session, device: str | None = None) -> list[Address]:
    stmt = select(Address).join(Address.subnet).order_by(Subnet.name, Address.ip)
    if device is not None:
        stmt = stmt.join(Address.device).where(Device.name == device)
    return list(session.scalars(stmt))
