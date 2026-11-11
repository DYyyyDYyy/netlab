import ipaddress
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from pydantic import IPvAnyNetwork
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import ipam, render, schemas
from app.config import get_settings
from app.db import get_session
from app.models import VLAN, VRF, Device
from app.observability import ALLOCATIONS, SubnetCollector, access_log, setup_logging

settings = get_settings()
setup_logging(settings.log_level)
REGISTRY.register(SubnetCollector())

app = FastAPI(title="netlab IPAM", version="0.1.0")
app.middleware("http")(access_log)

DB = Annotated[Session, Depends(get_session)]


@app.exception_handler(ipam.IPAMError)
async def ipam_error(_: Request, exc: ipam.IPAMError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)


def _commit(session: Session, obj: Any) -> Any:
    session.add(obj)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, str(exc.orig).splitlines()[0]) from exc
    return obj


# --- ops -------------------------------------------------------------------


@app.get("/health")
def health(session: DB) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok", "db": "ok"}


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


# --- inventory -------------------------------------------------------------


@app.post("/vrfs", status_code=201)
def create_vrf(data: schemas.VRFIn, session: DB) -> schemas.VRFOut:
    return schemas.VRFOut.model_validate(_commit(session, VRF(**data.model_dump())))


@app.get("/vrfs")
def list_vrfs(session: DB) -> list[schemas.VRFOut]:
    return [schemas.VRFOut.model_validate(v) for v in session.scalars(select(VRF))]


@app.post("/vlans", status_code=201)
def create_vlan(data: schemas.VLANIn, session: DB) -> schemas.VLANOut:
    return schemas.VLANOut.model_validate(_commit(session, VLAN(**data.model_dump())))


@app.get("/vlans")
def list_vlans(session: DB) -> list[schemas.VLANOut]:
    return [schemas.VLANOut.model_validate(v) for v in session.scalars(select(VLAN))]


@app.post("/devices", status_code=201)
def create_device(data: schemas.DeviceIn, session: DB) -> schemas.DeviceOut:
    return schemas.DeviceOut.model_validate(_commit(session, Device(**data.model_dump())))


@app.get("/devices")
def list_devices(session: DB) -> list[schemas.DeviceOut]:
    devices = session.scalars(select(Device).order_by(Device.name))
    return [schemas.DeviceOut.model_validate(d) for d in devices]


# --- IPAM ------------------------------------------------------------------


@app.post("/subnets", status_code=201)
def create_subnet(data: schemas.SubnetIn, session: DB) -> schemas.SubnetOut:
    return ipam.subnet_out(ipam.create_subnet(session, data))


@app.get("/subnets")
def list_subnets(session: DB) -> list[schemas.SubnetOut]:
    return [ipam.subnet_out(s) for s in ipam.list_subnets(session)]


@app.get("/subnets/overlaps")
def overlaps(
    session: DB, prefix: Annotated[IPvAnyNetwork, Query()], vrf: str = "default"
) -> schemas.OverlapOut:
    v = ipam.get_vrf(session, vrf)
    found = ipam.find_overlaps(session, v, ipaddress.ip_network(prefix))
    return schemas.OverlapOut(prefix=prefix, vrf=vrf, overlaps=[ipam.subnet_out(s) for s in found])


@app.get("/subnets/{name}")
def get_subnet(name: str, session: DB) -> schemas.SubnetOut:
    return ipam.subnet_out(ipam.get_subnet(session, name))


@app.post("/subnets/{name}/allocate", status_code=201)
def allocate(name: str, data: schemas.AllocateIn, session: DB) -> schemas.AddressOut:
    try:
        addr = ipam.allocate(session, name, data)
    except ipam.IPAMError:
        ALLOCATIONS.labels("failed").inc()
        raise
    ALLOCATIONS.labels("ok").inc()
    return ipam.address_out(addr)


@app.post("/subnets/{name}/addresses", status_code=201)
def assign(name: str, data: schemas.AddressIn, session: DB) -> schemas.AddressOut:
    return ipam.address_out(ipam.assign(session, name, data))


@app.get("/addresses")
def list_addresses(session: DB, device: str | None = None) -> list[schemas.AddressOut]:
    return [ipam.address_out(a) for a in ipam.list_addresses(session, device)]


# --- config generation -----------------------------------------------------


@app.get("/topology")
def topology(session: DB) -> dict[str, Any]:
    return render.topology(session)


@app.get("/render/frr/{device}", response_class=PlainTextResponse)
def render_frr(device: str, session: DB) -> str:
    return render.render_frr(session, device)


@app.get("/render/frr-daemons", response_class=PlainTextResponse)
def render_daemons() -> str:
    return render.render_daemons()


@app.get("/render/nftables/{device}", response_class=PlainTextResponse)
def render_nftables(device: str, session: DB) -> str:
    return render.render_nftables(session, device)


@app.get("/render/coredns/Corefile", response_class=PlainTextResponse)
def render_corefile() -> str:
    return render.render_corefile(settings.dns_zone)


@app.get("/render/coredns/zone", response_class=PlainTextResponse)
def render_zone(session: DB) -> str:
    return render.render_zone(session, settings.dns_zone)


@app.get("/render/coredns/reverse", response_class=PlainTextResponse)
def render_reverse(session: DB) -> str:
    return render.render_reverse_zone(session, settings.dns_zone)
