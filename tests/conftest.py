from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def migrated() -> None:
    """Apply Alembic migrations once (also proves the migration itself works)."""
    from app.db import get_engine

    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.exit(f"PostgreSQL is not reachable (run `make test`): {exc}", returncode=2)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    with get_engine().begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    command.upgrade(cfg, "head")


@pytest.fixture
def db(migrated: None) -> Iterator[None]:
    from app.db import get_engine

    yield
    with get_engine().begin() as conn:
        conn.execute(text("TRUNCATE address, subnet, device, vlan, vrf RESTART IDENTITY CASCADE"))


@pytest.fixture
def client(db: None) -> Iterator[TestClient]:
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def vrf(client: TestClient) -> str:
    assert client.post("/vrfs", json={"name": "default"}).status_code == 201
    return "default"
