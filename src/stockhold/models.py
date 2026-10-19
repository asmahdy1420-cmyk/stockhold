"""Database models.

Stock is tracked with two counters on the product row:

* ``on_hand``  - physical units in the warehouse.
* ``reserved`` - units held by active reservations.

``available = on_hand - reserved``. Keeping ``reserved`` as a counter (instead of summing
reservation lines on every request) means a reservation only has to lock the product rows
it touches, and the invariant ``0 <= reserved <= on_hand`` is enforced by a CHECK constraint.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    """Naive UTC timestamp; stored the same way on SQLite and PostgreSQL."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return uuid.uuid4().hex


class ReservationStatus(str, enum.Enum):
    ACTIVE = "active"
    CONFIRMED = "confirmed"
    RELEASED = "released"
    EXPIRED = "expired"


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("on_hand >= 0", name="ck_products_on_hand_non_negative"),
        CheckConstraint("reserved >= 0", name="ck_products_reserved_non_negative"),
        CheckConstraint("reserved <= on_hand", name="ck_products_reserved_le_on_hand"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        Index("ix_reservations_status_expires_at", "status", "expires_at"),
        Index("ix_reservations_cart_id", "cart_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    cart_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, name="reservation_status", native_enum=False, length=16),
        nullable=False,
        default=ReservationStatus.ACTIVE,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    items: Mapped[list[ReservationItem]] = relationship(
        back_populates="reservation",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ReservationItem.product_id",
    )


class ReservationItem(Base):
    __tablename__ = "reservation_items"
    __table_args__ = (
        UniqueConstraint("reservation_id", "product_id", name="uq_reservation_item_product"),
        CheckConstraint("quantity > 0", name="ck_reservation_items_quantity_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reservation_id: Mapped[str] = mapped_column(
        ForeignKey("reservations.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    reservation: Mapped[Reservation] = relationship(back_populates="items")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    reservation_id: Mapped[str] = mapped_column(
        ForeignKey("reservations.id"), unique=True, nullable=False
    )
    cart_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    reservation: Mapped[Reservation] = relationship(lazy="selectin")


class IdempotencyRecord(Base):
    """Remembers which order a given ``Idempotency-Key`` produced."""

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
