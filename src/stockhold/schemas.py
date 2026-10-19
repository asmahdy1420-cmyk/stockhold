"""Request and response bodies.

Pydantic evaluates these annotations at runtime, so optional fields use
``typing.Optional`` rather than the ``X | None`` spelling.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .models import ReservationStatus


class ProductCreate(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    on_hand: int = Field(default=0, ge=0)


class StockAdjustment(BaseModel):
    delta: int = Field(description="Positive to receive stock, negative to write it off.")


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku: str
    name: str
    on_hand: int
    reserved: int
    available: int


class ReservationLineIn(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)


class ReservationCreate(BaseModel):
    cart_id: str = Field(min_length=1, max_length=64)
    items: list[ReservationLineIn] = Field(min_length=1)
    ttl_seconds: Optional[int] = Field(default=None, gt=0)


class ReservationLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: int
    quantity: int


class ReservationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    cart_id: str
    status: ReservationStatus
    expires_at: datetime
    created_at: datetime
    closed_at: Optional[datetime] = None
    items: list[ReservationLineOut]


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    reservation_id: str
    cart_id: str
    created_at: datetime
    items: list[ReservationLineOut]


class ExpireResult(BaseModel):
    expired: int


class ErrorOut(BaseModel):
    error: str
    message: str
    details: dict = Field(default_factory=dict)
