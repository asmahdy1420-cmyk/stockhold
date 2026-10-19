"""Inventory operations.

Every function here expects to run inside one write transaction (see
``db.write_transaction``). Locks are always taken in the same order to rule out
deadlocks between concurrent requests:

1. the reservation row (when the operation targets an existing reservation),
2. the product rows, sorted by primary key.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import (
    Conflict,
    IdempotencyMismatch,
    InsufficientStock,
    InvalidState,
    NotFound,
    ReservationExpired,
)
from .models import (
    IdempotencyRecord,
    Order,
    Product,
    Reservation,
    ReservationItem,
    ReservationStatus,
    utcnow,
)

CLOSED_STATUSES = (
    ReservationStatus.CONFIRMED,
    ReservationStatus.RELEASED,
    ReservationStatus.EXPIRED,
)


@dataclass(frozen=True)
class LineRequest:
    product_id: int
    quantity: int


@dataclass(frozen=True)
class ConfirmResult:
    order: Order
    replayed: bool


# --------------------------------------------------------------------------- locking


def _lock_products(session: Session, product_ids: Iterable[int]) -> dict[int, Product]:
    ids = sorted(set(product_ids))
    stmt = (
        select(Product)
        .where(Product.id.in_(ids))
        .order_by(Product.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    products = {p.id: p for p in session.scalars(stmt)}
    missing = [pid for pid in ids if pid not in products]
    if missing:
        raise NotFound("Unknown product id(s).", product_ids=missing)
    return products


def _lock_reservation(session: Session, reservation_id: str) -> Reservation:
    stmt = (
        select(Reservation)
        .where(Reservation.id == reservation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    reservation = session.scalars(stmt).first()
    if reservation is None:
        raise NotFound("Reservation not found.", reservation_id=reservation_id)
    return reservation


# --------------------------------------------------------------------------- products


def create_product(session: Session, sku: str, name: str, on_hand: int) -> Product:
    if session.scalars(select(Product.id).where(Product.sku == sku)).first() is not None:
        raise Conflict("A product with this SKU already exists.", sku=sku)
    product = Product(sku=sku, name=name, on_hand=on_hand, reserved=0)
    session.add(product)
    session.flush()
    return product


def get_product(session: Session, product_id: int) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise NotFound("Product not found.", product_id=product_id)
    return product


def adjust_stock(session: Session, product_id: int, delta: int) -> Product:
    """Receive (positive delta) or write off (negative delta) physical stock."""
    product = _lock_products(session, [product_id])[product_id]
    new_on_hand = product.on_hand + delta
    if new_on_hand < product.reserved:
        raise InsufficientStock(
            "Adjustment would drop stock below what is already reserved.",
            product_id=product_id,
            on_hand=product.on_hand,
            reserved=product.reserved,
            delta=delta,
        )
    product.on_hand = new_on_hand
    return product


# --------------------------------------------------------------------------- reservations


def _merge_lines(lines: Sequence[LineRequest]) -> dict[int, int]:
    merged: dict[int, int] = {}
    for line in lines:
        if line.quantity <= 0:
            raise Conflict("Quantities must be positive.", product_id=line.product_id)
        merged[line.product_id] = merged.get(line.product_id, 0) + line.quantity
    if not merged:
        raise Conflict("A reservation needs at least one line.")
    return merged


def reserve(
    session: Session,
    cart_id: str,
    lines: Sequence[LineRequest],
    ttl_seconds: int,
    now: datetime | None = None,
) -> Reservation:
    """Hold stock for a cart. All lines succeed together or none do."""
    now = now or utcnow()
    wanted = _merge_lines(lines)
    products = _lock_products(session, wanted)

    shortages = [
        {"product_id": pid, "requested": qty, "available": products[pid].available}
        for pid, qty in wanted.items()
        if products[pid].available < qty
    ]
    if shortages:
        raise InsufficientStock("Not enough stock for one or more lines.", lines=shortages)

    reservation = Reservation(
        cart_id=cart_id,
        status=ReservationStatus.ACTIVE,
        expires_at=now + timedelta(seconds=ttl_seconds),
        created_at=now,
    )
    for pid in sorted(wanted):
        products[pid].reserved += wanted[pid]
        reservation.items.append(ReservationItem(product_id=pid, quantity=wanted[pid]))
    session.add(reservation)
    session.flush()
    return reservation


def get_reservation(session: Session, reservation_id: str) -> Reservation:
    reservation = session.get(Reservation, reservation_id)
    if reservation is None:
        raise NotFound("Reservation not found.", reservation_id=reservation_id)
    return reservation


def _return_stock(session: Session, reservation: Reservation) -> None:
    products = _lock_products(session, (item.product_id for item in reservation.items))
    for item in reservation.items:
        products[item.product_id].reserved -= item.quantity


def _close(
    session: Session, reservation: Reservation, status: ReservationStatus, now: datetime
) -> None:
    _return_stock(session, reservation)
    reservation.status = status
    reservation.closed_at = now


def release(session: Session, reservation_id: str, now: datetime | None = None) -> Reservation:
    """Give held stock back, e.g. when the shopper empties the cart.

    Releasing an already released or expired reservation is a no-op, so clients can
    retry safely. Releasing a confirmed reservation is an error: that stock is sold.
    """
    now = now or utcnow()
    reservation = _lock_reservation(session, reservation_id)
    if reservation.status is ReservationStatus.CONFIRMED:
        raise InvalidState("Reservation is already confirmed.", reservation_id=reservation_id)
    if reservation.status is ReservationStatus.ACTIVE:
        _close(session, reservation, ReservationStatus.RELEASED, now)
    return reservation


# --------------------------------------------------------------------------- confirmation


def fingerprint(reservation_id: str) -> str:
    return hashlib.sha256(f"confirm:{reservation_id}".encode()).hexdigest()


def _replay(session: Session, key: str, expected: str) -> ConfirmResult | None:
    record = session.get(IdempotencyRecord, key, populate_existing=True)
    if record is None:
        return None
    if record.request_fingerprint != expected:
        raise IdempotencyMismatch(
            "This Idempotency-Key was already used for a different request.", key=key
        )
    order = session.get(Order, record.order_id)
    assert order is not None
    return ConfirmResult(order=order, replayed=True)


def confirm(
    session: Session,
    reservation_id: str,
    idempotency_key: str,
    now: datetime | None = None,
) -> ConfirmResult:
    """Turn an active reservation into an order, exactly once per key.

    * Same key, same reservation  -> the original order is returned (a replay).
    * Same key, other reservation -> ``IdempotencyMismatch``.
    * New key, already confirmed  -> ``InvalidState``.
    """
    now = now or utcnow()
    expected = fingerprint(reservation_id)

    replay = _replay(session, idempotency_key, expected)
    if replay is not None:
        return replay

    reservation = _lock_reservation(session, reservation_id)

    # A concurrent request with the same key may have committed while we waited for
    # the lock. Under READ COMMITTED this second lookup sees its row.
    replay = _replay(session, idempotency_key, expected)
    if replay is not None:
        return replay

    if reservation.status is ReservationStatus.CONFIRMED:
        raise InvalidState("Reservation is already confirmed.", reservation_id=reservation_id)
    if reservation.status in CLOSED_STATUSES or reservation.expires_at <= now:
        raise ReservationExpired(
            "Reservation is no longer active.",
            reservation_id=reservation_id,
            status=reservation.status.value,
        )

    products = _lock_products(session, (item.product_id for item in reservation.items))
    for item in reservation.items:
        product = products[item.product_id]
        product.reserved -= item.quantity
        product.on_hand -= item.quantity

    reservation.status = ReservationStatus.CONFIRMED
    reservation.closed_at = now
    order = Order(reservation_id=reservation.id, cart_id=reservation.cart_id, created_at=now)
    session.add(order)
    session.flush()
    session.add(
        IdempotencyRecord(
            key=idempotency_key, request_fingerprint=expected, order_id=order.id, created_at=now
        )
    )
    session.flush()
    return ConfirmResult(order=order, replayed=False)


# --------------------------------------------------------------------------- expiry


def expire_stale(session: Session, now: datetime | None = None, batch_size: int = 500) -> int:
    """Expire active reservations whose TTL has passed. Returns how many were expired.

    ``SKIP LOCKED`` lets several sweepers run side by side, and keeps the sweeper from
    blocking on a reservation that a shopper is confirming at this moment.
    """
    now = now or utcnow()
    stmt = (
        select(Reservation)
        .where(
            Reservation.status == ReservationStatus.ACTIVE,
            Reservation.expires_at <= now,
        )
        .order_by(Reservation.expires_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    stale = list(session.scalars(stmt))
    for reservation in stale:
        _close(session, reservation, ReservationStatus.EXPIRED, now)
    return len(stale)
