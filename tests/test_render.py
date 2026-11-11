"""Snapshot tests: the lab seed rendered through the API must match tests/snapshots.

Regenerate after an intentional change with ``UPDATE_SNAPSHOTS=1 make test``.
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lab.seed import seed

SNAPSHOTS = Path(__file__).parent / "snapshots"

CASES = {
    "frr-r1.conf": "/render/frr/r1",
    "frr-r2.conf": "/render/frr/r2",
    "frr-border.conf": "/render/frr/border",
    "nftables-border.conf": "/render/nftables/border",
    "Corefile": "/render/coredns/Corefile",
    "db.netlab.internal": "/render/coredns/zone",
    "db.in-addr.arpa": "/render/coredns/reverse",
}


@pytest.fixture
def seeded(client: TestClient) -> TestClient:
    seed(client)
    seed(client)  # idempotent
    return client


@pytest.mark.parametrize(("snapshot", "url"), CASES.items())
def test_snapshot(seeded: TestClient, snapshot: str, url: str) -> None:
    r = seeded.get(url)
    assert r.status_code == 200, r.text
    path = SNAPSHOTS / snapshot
    if os.environ.get("UPDATE_SNAPSHOTS"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(r.text)
    assert r.text == path.read_text()


def test_seed_allocations(seeded: TestClient) -> None:
    ips = {a["device"]: a["ip"] for a in seeded.get("/addresses").json() if a["subnet"] != "wan"}
    assert ips["host1"] == "10.10.1.2"
    assert ips["host2"] == "10.10.1.3"
    assert ips["srv1"] == "10.10.2.2"


def test_render_rejects_non_router(seeded: TestClient) -> None:
    assert seeded.get("/render/frr/host1").status_code == 422
    assert seeded.get("/render/nftables/r1").status_code == 422  # no wan interface
    assert seeded.get("/render/frr/ghost").status_code == 404


def test_topology(seeded: TestClient) -> None:
    topo = seeded.get("/topology").json()
    assert "loopbacks" not in {n["name"] for n in topo["networks"]}
    host1 = next(d for d in topo["devices"] if d["name"] == "host1")
    assert host1["routes"] == [{"dst": "default", "via": "10.10.1.1"}]
    isp = next(d for d in topo["devices"] if d["name"] == "isp")
    assert {"dst": "10.10.1.0/24", "via": "203.0.113.1"} in isp["routes"]
