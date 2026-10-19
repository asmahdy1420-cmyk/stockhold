from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from stockhold.api import create_app
from stockhold.config import Settings
from stockhold.db import Base, make_engine, make_session_factory


class FakeClock:
    """Controllable clock so TTL behaviour is tested without sleeping."""

    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, 0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _database_url(tmp_path) -> str:
    # Set STOCKHOLD_TEST_DATABASE_URL to a PostgreSQL URL to run the suite against it.
    return os.environ.get("STOCKHOLD_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(database_url=_database_url(tmp_path), default_ttl_seconds=600)


@pytest.fixture
def engine(settings):
    engine = make_engine(settings.database_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(settings, session_factory, clock):
    app = create_app(settings=settings, session_factory=session_factory, clock=clock)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def make_product(client):
    counter = {"n": 0}

    def _make(on_hand: int = 10, sku: str | None = None) -> dict:
        counter["n"] += 1
        payload = {
            "sku": sku or f"SKU-{counter['n']:03d}",
            "name": f"Product {counter['n']}",
            "on_hand": on_hand,
        }
        response = client.post("/products", json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    return _make


@pytest.fixture
def reserve(client):
    def _reserve(lines: list[tuple[int, int]], cart_id: str = "cart-1", ttl: int | None = None):
        body: dict = {
            "cart_id": cart_id,
            "items": [{"product_id": pid, "quantity": qty} for pid, qty in lines],
        }
        if ttl is not None:
            body["ttl_seconds"] = ttl
        return client.post("/reservations", json=body)

    return _reserve
