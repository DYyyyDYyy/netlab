from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress, IPvAnyNetwork

from app.models import DeviceKind, SubnetRole

NAME = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
IFNAME = Field(default=None, pattern=r"^[a-zA-Z0-9_.-]{1,15}$")


class VRFIn(BaseModel):
    name: str = NAME
    rd: str | None = None


class VRFOut(VRFIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


class VLANIn(BaseModel):
    vid: int = Field(ge=1, le=4094)
    name: str = NAME


class VLANOut(VLANIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


class DeviceIn(BaseModel):
    name: str = NAME
    kind: DeviceKind


class DeviceOut(DeviceIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


class SubnetIn(BaseModel):
    name: str = NAME
    vrf: str = "default"
    prefix: IPvAnyNetwork
    role: SubnetRole
    vlan: int | None = Field(default=None, ge=1, le=4094, description="VLAN id (vid)")
    gateway: IPvAnyAddress | None = None


class SubnetOut(BaseModel):
    id: int
    name: str
    vrf: str
    prefix: IPvAnyNetwork
    role: SubnetRole
    vlan: int | None
    gateway: IPvAnyAddress | None
    size: int
    used: int
    utilization: float


class AllocateIn(BaseModel):
    device: str | None = None
    interface: str | None = IFNAME
    dns_name: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    description: str | None = None


class AddressIn(AllocateIn):
    ip: IPvAnyAddress


class AddressOut(BaseModel):
    id: int
    subnet: str
    ip: IPvAnyAddress
    prefixlen: int
    device: str | None
    interface: str | None
    dns_name: str | None
    description: str | None


class OverlapOut(BaseModel):
    prefix: IPvAnyNetwork
    vrf: str
    overlaps: list[SubnetOut]
