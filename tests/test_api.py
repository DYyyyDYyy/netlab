from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


def subnet(client: TestClient, name: str, prefix: str, **kw: object) -> object:
    return client.post("/subnets", json={"name": name, "prefix": prefix, "role": "lan", **kw})


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok", "db": "ok"}


def test_create_and_list_subnet(client: TestClient, vrf: str) -> None:
    r = subnet(client, "lan", "10.1.0.0/24", gateway="10.1.0.1")
    assert r.status_code == 201, r.text
    assert r.json()["size"] == 254
    assert [s["prefix"] for s in client.get("/subnets").json()] == ["10.1.0.0/24"]


def test_host_bits_rejected(client: TestClient, vrf: str) -> None:
    assert subnet(client, "bad", "10.1.0.5/24").status_code == 422


def test_gateway_outside_rejected(client: TestClient, vrf: str) -> None:
    assert subnet(client, "lan", "10.1.0.0/24", gateway="10.2.0.1").status_code == 422


@pytest.mark.parametrize("other", ["10.1.0.0/24", "10.1.0.128/25", "10.0.0.0/8"])
def test_overlap_rejected(client: TestClient, vrf: str, other: str) -> None:
    assert subnet(client, "a", "10.1.0.0/24").status_code == 201
    r = subnet(client, "b", other)
    assert r.status_code == 409
    assert "overlaps" in r.json()["detail"]


def test_adjacent_prefixes_allowed(client: TestClient, vrf: str) -> None:
    assert subnet(client, "a", "10.1.0.0/25").status_code == 201
    assert subnet(client, "b", "10.1.0.128/25").status_code == 201


def test_same_prefix_in_other_vrf_allowed(client: TestClient, vrf: str) -> None:
    client.post("/vrfs", json={"name": "customer-a"})
    assert subnet(client, "a", "10.1.0.0/24").status_code == 201
    assert subnet(client, "b", "10.1.0.0/24", vrf="customer-a").status_code == 201


def test_overlap_search(client: TestClient, vrf: str) -> None:
    subnet(client, "a", "10.1.0.0/24")
    subnet(client, "b", "10.1.1.0/24")
    subnet(client, "c", "10.2.0.0/24")
    r = client.get("/subnets/overlaps", params={"prefix": "10.1.0.0/16"}).json()
    assert [s["name"] for s in r["overlaps"]] == ["a", "b"]


def test_db_exclude_constraint_without_app(client: TestClient, vrf: str) -> None:
    """The guarantee lives in PostgreSQL, not only in the API pre-check."""
    from app.db import get_engine

    subnet(client, "a", "10.1.0.0/24")
    with pytest.raises(IntegrityError, match="subnet_no_overlap"), get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO subnet (name, vrf_id, prefix, role) "
                "VALUES ('raw', 1, '10.1.0.64/26', 'lan')"
            )
        )


def test_allocate_sequential_skips_gateway(client: TestClient, vrf: str) -> None:
    subnet(client, "lan", "10.1.0.0/24", gateway="10.1.0.1")
    ips = [client.post("/subnets/lan/allocate", json={}).json()["ip"] for _ in range(3)]
    assert ips == ["10.1.0.2", "10.1.0.3", "10.1.0.4"]


def test_allocate_exhaustion(client: TestClient, vrf: str) -> None:
    subnet(client, "p2p", "10.9.0.0/30")
    assert client.post("/subnets/p2p/allocate", json={}).json()["ip"] == "10.9.0.1"
    assert client.post("/subnets/p2p/allocate", json={}).json()["ip"] == "10.9.0.2"
    r = client.post("/subnets/p2p/allocate", json={})
    assert r.status_code == 409
    assert "exhausted" in r.json()["detail"]
    assert client.get("/subnets/p2p").json()["utilization"] == 1.0


def test_allocate_ipv6(client: TestClient, vrf: str) -> None:
    assert subnet(client, "v6", "2001:db8:10::/64").status_code == 201
    assert client.get("/subnets/v6").json()["size"] == 2**64 - 1
    a = client.post("/subnets/v6/allocate", json={"dns_name": "v6host"}).json()
    assert a["ip"] == "2001:db8:10::1"
    assert a["prefixlen"] == 64
    assert subnet(client, "v6b", "2001:db8:10:0:8000::/65").status_code == 409


def test_ipv6_exhaustion_126(client: TestClient, vrf: str) -> None:
    subnet(client, "v6", "2001:db8::/126")
    got = [client.post("/subnets/v6/allocate", json={}).status_code for _ in range(4)]
    assert got == [201, 201, 201, 409]


def test_assign_validation(client: TestClient, vrf: str) -> None:
    subnet(client, "lan", "10.1.0.0/24")
    ok = client.post("/subnets/lan/addresses", json={"ip": "10.1.0.10"})
    assert ok.status_code == 201
    assert client.post("/subnets/lan/addresses", json={"ip": "10.1.0.10"}).status_code == 409
    assert client.post("/subnets/lan/addresses", json={"ip": "10.2.0.1"}).status_code == 422
    assert client.post("/subnets/lan/addresses", json={"ip": "10.1.0.0"}).status_code == 422
    assert client.post("/subnets/lan/addresses", json={"ip": "10.1.0.255"}).status_code == 422


def test_unknown_device_404(client: TestClient, vrf: str) -> None:
    subnet(client, "lan", "10.1.0.0/24")
    assert client.post("/subnets/lan/allocate", json={"device": "nope"}).status_code == 404


def test_concurrent_allocations_are_unique(client: TestClient, vrf: str) -> None:
    from app import ipam, schemas
    from app.db import get_sessionmaker

    subnet(client, "lan", "10.1.0.0/27")

    def one(_: int) -> str:
        with get_sessionmaker()() as s:
            return str(ipam.allocate(s, "lan", schemas.AllocateIn()).ip)

    with ThreadPoolExecutor(max_workers=8) as pool:
        ips = list(pool.map(one, range(30)))
    assert len(set(ips)) == 30


def test_metrics(client: TestClient, vrf: str) -> None:
    subnet(client, "p2p", "10.9.0.0/30")
    client.post("/subnets/p2p/allocate", json={})
    body = client.get("/metrics").text
    assert 'netlab_subnet_utilization_ratio{prefix="10.9.0.0/30",subnet="p2p"} 0.5' in body
    assert 'netlab_ip_allocations_total{result="ok"}' in body
    assert "netlab_http_requests_total" in body
