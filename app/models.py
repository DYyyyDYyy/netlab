"""IPAM data model.

Prefixes are stored as PostgreSQL ``cidr`` and addresses as ``inet``; overlapping
subnets inside one VRF are rejected by the database itself via an
``EXCLUDE USING gist (vrf_id WITH =, prefix inet_ops WITH &&)`` constraint.
"""

import enum
import ipaddress
from typing import Any

from sqlalchemy import CheckConstraint, Dialect, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class CIDR(TypeDecorator[IPNetwork]):
    """``cidr`` column that always round-trips as ``ipaddress`` network objects."""

    impl = postgresql.CIDR
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        return None if value is None else str(ipaddress.ip_network(value))

    def process_result_value(self, value: Any, dialect: Dialect) -> IPNetwork | None:
        return None if value is None else ipaddress.ip_network(str(value))


class INET(TypeDecorator[IPAddress]):
    """``inet`` column holding a single host address (no prefix length)."""

    impl = postgresql.INET
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        return None if value is None else str(ipaddress.ip_address(value))

    def process_result_value(self, value: Any, dialect: Dialect) -> IPAddress | None:
        return None if value is None else ipaddress.ip_interface(str(value)).ip


class Base(DeclarativeBase):
    pass


class SubnetRole(enum.StrEnum):
    lan = "lan"
    dmz = "dmz"
    transit = "transit"
    loopback = "loopback"
    wan = "wan"


class DeviceKind(enum.StrEnum):
    router = "router"
    host = "host"
    server = "server"
    dns = "dns"
    external = "external"


class VRF(Base):
    __tablename__ = "vrf"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    rd: Mapped[str | None] = mapped_column(String(32))


class VLAN(Base):
    __tablename__ = "vlan"
    __table_args__ = (CheckConstraint("vid BETWEEN 1 AND 4094", name="vlan_vid_range"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vid: Mapped[int] = mapped_column(unique=True)
    name: Mapped[str] = mapped_column(String(64))


class Device(Base):
    __tablename__ = "device"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(63), unique=True)
    kind: Mapped[DeviceKind] = mapped_column(Enum(DeviceKind, name="device_kind"))

    addresses: Mapped[list["Address"]] = relationship(back_populates="device")


class Subnet(Base):
    __tablename__ = "subnet"
    __table_args__ = (
        ExcludeConstraint(
            ("vrf_id", "="),
            ("prefix", "&&"),
            using="gist",
            name="subnet_no_overlap",
            ops={"prefix": "inet_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(63), unique=True)
    vrf_id: Mapped[int] = mapped_column(ForeignKey("vrf.id"))
    prefix: Mapped[IPNetwork] = mapped_column(CIDR)
    role: Mapped[SubnetRole] = mapped_column(Enum(SubnetRole, name="subnet_role"))
    vlan_id: Mapped[int | None] = mapped_column(ForeignKey("vlan.id"))
    gateway: Mapped[IPAddress | None] = mapped_column(INET)

    vrf: Mapped[VRF] = relationship()
    vlan: Mapped[VLAN | None] = relationship()
    addresses: Mapped[list["Address"]] = relationship(
        back_populates="subnet", order_by="Address.ip", cascade="all, delete-orphan"
    )


class Address(Base):
    __tablename__ = "address"
    __table_args__ = (UniqueConstraint("subnet_id", "ip", name="address_unique_in_subnet"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    subnet_id: Mapped[int] = mapped_column(ForeignKey("subnet.id", ondelete="CASCADE"))
    ip: Mapped[IPAddress] = mapped_column(INET)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("device.id"))
    interface: Mapped[str | None] = mapped_column(String(15))
    dns_name: Mapped[str | None] = mapped_column(String(63))
    description: Mapped[str | None] = mapped_column(String(255))

    subnet: Mapped[Subnet] = relationship(back_populates="addresses")
    device: Mapped[Device | None] = relationship(back_populates="addresses")
