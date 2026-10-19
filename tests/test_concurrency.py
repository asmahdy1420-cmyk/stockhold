"""Many shoppers, few units: the service must never oversell.

These tests call the service layer from real threads. On SQLite the app-level write
lock serialises them; on PostgreSQL (STOCKHOLD_TEST_DATABASE_URL) the row locks do.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from stockhold import services
from stockhold.cli import main
from stockhold.db import write_transaction
from stockhold.errors import InsufficientStock
from stockhold.models import Order, Product, Reservation, ReservationStatus, utcnow


def _seed_product(session_factory, on_hand: int) -> int:
    with write_transaction(session_factory) as session:
        return services.create_product(session, "HOT-ITEM", "Limited drop", on_hand).id


def test_parallel_reservations_never_oversell(session_factory):
    product_id = _seed_product(session_factory, on_hand=5)

    def attempt(n: int) -> bool:
        try:
            with write_transaction(session_factory) as session:
                line = services.LineRequest(product_id, 1)
                services.reserve(session, f"cart-{n}", [line], ttl_seconds=300)
            return True
        except InsufficientStock:
            return False

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(attempt, range(40)))

    assert results.count(True) == 5
    with session_factory() as session:
        product = session.get(Product, product_id)
        assert (product.on_hand, product.reserved) == (5, 5)


def test_parallel_confirms_with_same_key_create_one_order(session_factory):
    product_id = _seed_product(session_factory, on_hand=3)
    with write_transaction(session_factory) as session:
        line = services.LineRequest(product_id, 2)
        reservation_id = services.reserve(session, "cart", [line], ttl_seconds=300).id

    def attempt(_: int) -> str:
        with write_transaction(session_factory) as session:
            return services.confirm(session, reservation_id, "double-click").order.id

    with ThreadPoolExecutor(max_workers=8) as pool:
        order_ids = set(pool.map(attempt, range(16)))

    assert len(order_ids) == 1
    with session_factory() as session:
        assert session.query(Order).count() == 1
        assert session.get(Product, product_id).on_hand == 1


def test_cli_expire_command_releases_stale_holds(settings, session_factory, capsys, monkeypatch):
    product_id = _seed_product(session_factory, on_hand=4)
    past = utcnow() - timedelta(hours=1)
    with write_transaction(session_factory) as session:
        line = services.LineRequest(product_id, 4)
        services.reserve(session, "abandoned", [line], ttl_seconds=60, now=past)

    monkeypatch.setenv("STOCKHOLD_DATABASE_URL", settings.database_url)
    assert main(["expire"]) == 0

    assert "expired 1 reservation(s)" in capsys.readouterr().out
    with session_factory() as session:
        assert session.get(Product, product_id).reserved == 0
        statuses = {r.status for r in session.query(Reservation)}
        assert statuses == {ReservationStatus.EXPIRED}
