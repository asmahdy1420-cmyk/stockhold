"""HTTP layer. Thin by design: parse, call a service in one transaction, serialise."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from . import __version__, services
from .config import Settings
from .db import Base, make_engine, make_session_factory, write_transaction
from .errors import StockholdError
from .models import Order, Reservation, utcnow
from .schemas import (
    ErrorOut,
    ExpireResult,
    OrderOut,
    ProductCreate,
    ProductOut,
    ReservationCreate,
    ReservationLineOut,
    ReservationOut,
    StockAdjustment,
)

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replayed"


def _order_out(order: Order) -> OrderOut:
    return OrderOut(
        id=order.id,
        reservation_id=order.reservation_id,
        cart_id=order.cart_id,
        created_at=order.created_at,
        items=[ReservationLineOut.model_validate(i) for i in order.reservation.items],
    )


def _reservation_out(reservation: Reservation) -> ReservationOut:
    return ReservationOut.model_validate(reservation)


def create_app(
    settings: Optional[Settings] = None,
    session_factory: Optional[sessionmaker[Session]] = None,
    clock: Callable[[], datetime] = utcnow,
    create_tables: bool = True,
) -> FastAPI:
    settings = settings or Settings.from_env()
    if session_factory is None:
        engine = make_engine(settings.database_url)
        if create_tables:
            Base.metadata.create_all(engine)
        session_factory = make_session_factory(engine)

    app = FastAPI(
        title="stockhold",
        version=__version__,
        description="Concurrency-safe inventory reservations for e-commerce carts.",
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.clock = clock

    def read_session(request: Request) -> Iterator[Session]:
        session = request.app.state.session_factory()
        try:
            yield session
        finally:
            session.close()

    def tx(request: Request):
        return write_transaction(request.app.state.session_factory)

    def now(request: Request) -> datetime:
        return request.app.state.clock()

    @app.exception_handler(StockholdError)
    async def _domain_error(_request: Request, exc: StockholdError) -> JSONResponse:
        body = ErrorOut(error=exc.code, message=exc.message, details=exc.details)
        return JSONResponse(status_code=exc.status_code, content=body.model_dump())

    @app.exception_handler(IntegrityError)
    async def _integrity_error(_request: Request, _exc: IntegrityError) -> JSONResponse:
        # Raced past a pre-check (for example two creates with the same SKU).
        body = ErrorOut(error="conflict", message="The request conflicts with existing data.")
        return JSONResponse(status_code=409, content=body.model_dump())

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    # ------------------------------------------------------------------ products

    @app.post("/products", status_code=201, response_model=ProductOut, tags=["products"])
    def create_product(body: ProductCreate, request: Request) -> ProductOut:
        with tx(request) as session:
            product = services.create_product(session, body.sku, body.name, body.on_hand)
            return ProductOut.model_validate(product)

    @app.get("/products/{product_id}", response_model=ProductOut, tags=["products"])
    def get_product(product_id: int, session: Session = Depends(read_session)) -> ProductOut:
        return ProductOut.model_validate(services.get_product(session, product_id))

    @app.post("/products/{product_id}/adjustments", response_model=ProductOut, tags=["products"])
    def adjust_stock(product_id: int, body: StockAdjustment, request: Request) -> ProductOut:
        with tx(request) as session:
            product = services.adjust_stock(session, product_id, body.delta)
            return ProductOut.model_validate(product)

    # ------------------------------------------------------------------ reservations

    @app.post(
        "/reservations", status_code=201, response_model=ReservationOut, tags=["reservations"]
    )
    def create_reservation(body: ReservationCreate, request: Request) -> ReservationOut:
        cfg: Settings = request.app.state.settings
        ttl = body.ttl_seconds or cfg.default_ttl_seconds
        if ttl > cfg.max_ttl_seconds:
            raise StockholdError(
                "ttl_seconds is above the allowed maximum.", max_ttl_seconds=cfg.max_ttl_seconds
            )
        lines = [services.LineRequest(i.product_id, i.quantity) for i in body.items]
        with tx(request) as session:
            reservation = services.reserve(session, body.cart_id, lines, ttl, now(request))
            return _reservation_out(reservation)

    @app.get("/reservations/{reservation_id}", response_model=ReservationOut, tags=["reservations"])
    def get_reservation(
        reservation_id: str, session: Session = Depends(read_session)
    ) -> ReservationOut:
        return _reservation_out(services.get_reservation(session, reservation_id))

    @app.post(
        "/reservations/{reservation_id}/release",
        response_model=ReservationOut,
        tags=["reservations"],
    )
    def release_reservation(reservation_id: str, request: Request) -> ReservationOut:
        with tx(request) as session:
            reservation = services.release(session, reservation_id, now(request))
            return _reservation_out(reservation)

    @app.post(
        "/reservations/{reservation_id}/confirm",
        status_code=201,
        response_model=OrderOut,
        tags=["orders"],
        responses={200: {"model": OrderOut, "description": "Replay of an earlier confirm."}},
    )
    def confirm_reservation(
        reservation_id: str,
        request: Request,
        response: Response,
        idempotency_key: Optional[str] = Header(default=None, alias=IDEMPOTENCY_HEADER),
    ) -> OrderOut:
        key = (idempotency_key or "").strip()
        if not key or len(key) > 255:
            raise StockholdError(
                f"A non-empty {IDEMPOTENCY_HEADER} header (max 255 chars) is required."
            )
        with tx(request) as session:
            result = services.confirm(session, reservation_id, key, now(request))
            out = _order_out(result.order)
        if result.replayed:
            response.status_code = 200
            response.headers[REPLAY_HEADER] = "true"
        return out

    # ------------------------------------------------------------------ ops

    @app.post("/admin/expire", response_model=ExpireResult, tags=["ops"])
    def expire(request: Request) -> ExpireResult:
        with tx(request) as session:
            return ExpireResult(expired=services.expire_stale(session, now(request)))

    return app
