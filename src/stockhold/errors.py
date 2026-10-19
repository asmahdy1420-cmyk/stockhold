"""Domain errors. The API layer maps each one to an HTTP status code."""

from __future__ import annotations


class StockholdError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFound(StockholdError):
    status_code = 404
    code = "not_found"


class Conflict(StockholdError):
    status_code = 409
    code = "conflict"


class InsufficientStock(Conflict):
    code = "insufficient_stock"


class InvalidState(Conflict):
    code = "invalid_state"


class ReservationExpired(StockholdError):
    status_code = 410
    code = "reservation_expired"


class IdempotencyMismatch(StockholdError):
    status_code = 422
    code = "idempotency_key_reused"
