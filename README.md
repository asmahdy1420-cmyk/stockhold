# stockhold

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

A small inventory reservation service for e-commerce checkouts, built with FastAPI,
SQLAlchemy 2 and PostgreSQL.

When a shopper starts checkout, `stockhold` holds the items in their cart for a limited
time. The hold either becomes an order (confirm), is given back (release), or lapses on
its own (expire). Two shoppers can never be sold the last unit, and a retried "Place
order" click never creates a second order.

## Screenshots

Five units in stock: a cart reserves two, a second cart asking for four gets a 409 with the short line, the first cart confirms into an order, and a repeated confirm with the same `Idempotency-Key` is replayed. Run locally with docker compose, using throwaway data.

![Reserve, conflict, confirm and idempotent replay](docs/screenshots/reserve-confirm-flow.png)

The endpoints listed in the interactive API docs at `/docs`.

![Swagger UI](docs/screenshots/api-docs.png)

## Why

Overselling usually comes from a read-then-write gap: two requests both read "1 left",
both decide that is enough, and both write. Double orders come from retries: the client
times out, retries, and the server runs the side effect twice. This project shows the
patterns I use for both problems, in a codebase small enough to read in one sitting:

- row locks (`SELECT ... FOR UPDATE`) taken in a fixed order,
- a `reserved` counter guarded by database CHECK constraints,
- reservations with a TTL and a background sweeper using `SKIP LOCKED`,
- an `Idempotency-Key` header on the one endpoint that creates money-relevant state.

## Architecture

```mermaid
flowchart LR
    C["Storefront / checkout"] -->|"REST + Idempotency-Key"| API["FastAPI app<br>api.py"]
    API --> S["Service layer<br>services.py"]
    S -->|"SELECT ... FOR UPDATE"| PG[("PostgreSQL")]
    SW["Sweeper<br>stockhold expire --every 30"] -->|"FOR UPDATE SKIP LOCKED"| PG
```

Reservation lifecycle:

```mermaid
stateDiagram-v2
    [*] --> active: POST /reservations
    active --> confirmed: POST /confirm (Idempotency-Key)
    active --> released: POST /release
    active --> expired: TTL passed + sweeper
    confirmed --> [*]
    released --> [*]
    expired --> [*]
```

| Module | Responsibility |
| --- | --- |
| `models.py` | `Product`, `Reservation`, `ReservationItem`, `Order`, `IdempotencyRecord` |
| `services.py` | All stock rules and locking; one function per operation |
| `api.py` | HTTP mapping, error translation, transaction boundaries |
| `db.py` | Engine/session setup and the write-transaction helper |
| `cli.py` | `init-db`, `expire`, `serve` |

## Quick start

With Docker (API, PostgreSQL and the expiry sweeper):

```bash
docker compose up --build
# API on http://localhost:8000, OpenAPI docs on http://localhost:8000/docs
```

Locally with SQLite:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
stockhold serve --port 8000
```

Configuration is read from the environment:

| Variable | Default |
| --- | --- |
| `STOCKHOLD_DATABASE_URL` | `sqlite:///./stockhold.db` |
| `STOCKHOLD_DEFAULT_TTL_SECONDS` | `900` |
| `STOCKHOLD_MAX_TTL_SECONDS` | `3600` |

For PostgreSQL use `postgresql+psycopg://user:pass@host:5432/db` and install the
`postgres` extra.

## API examples

Create a product with 5 units:

```bash
curl -s -X POST localhost:8000/products \
  -H 'Content-Type: application/json' \
  -d '{"sku": "MUG-BLUE", "name": "Blue mug", "on_hand": 5}'
# {"id":1,"sku":"MUG-BLUE","name":"Blue mug","on_hand":5,"reserved":0,"available":5}
```

Receive or write off stock:

```bash
curl -s -X POST localhost:8000/products/1/adjustments \
  -H 'Content-Type: application/json' -d '{"delta": 10}'
```

Reserve stock for a cart for 10 minutes:

```bash
curl -s -X POST localhost:8000/reservations \
  -H 'Content-Type: application/json' \
  -d '{"cart_id": "cart-42", "ttl_seconds": 600,
       "items": [{"product_id": 1, "quantity": 2}]}'
# {"id":"3f1c...","status":"active","expires_at":"...","items":[{"product_id":1,"quantity":2}], ...}
```

Not enough stock gives `409` with the lines that failed:

```json
{"error": "insufficient_stock", "message": "Not enough stock for one or more lines.",
 "details": {"lines": [{"product_id": 1, "requested": 50, "available": 13}]}}
```

Confirm the reservation into an order. The key is chosen by the client, once per
checkout attempt:

```bash
curl -s -i -X POST localhost:8000/reservations/3f1c.../confirm \
  -H 'Idempotency-Key: checkout-7d2a1e'
# HTTP/1.1 201 Created
```

Sending the same request again returns the same order with `200` and
`Idempotent-Replayed: true`. Reusing the key for a different reservation returns `422`.

Release a reservation (safe to repeat):

```bash
curl -s -X POST localhost:8000/reservations/3f1c.../release
```

Expire stale reservations, from the CLI or over HTTP:

```bash
stockhold expire                 # one sweep
stockhold expire --every 30      # keep sweeping
curl -s -X POST localhost:8000/admin/expire
```

### Status codes

| Code | Meaning |
| --- | --- |
| `201` | Product, reservation or order created |
| `200` | Read, release, adjustment, or idempotent replay of a confirm |
| `400` | Missing `Idempotency-Key`, TTL above the maximum |
| `404` | Unknown product or reservation |
| `409` | Insufficient stock, duplicate SKU, reservation already confirmed |
| `410` | Reservation expired or released before confirm |
| `422` | Invalid body, or `Idempotency-Key` reused for another reservation |

## Design decisions

**Counter plus row lock, not a sum query.** Each product row stores `on_hand` and
`reserved`; `available` is the difference. A reservation locks only the product rows it
touches with `SELECT ... FOR UPDATE`, checks availability, and bumps `reserved`. The
database enforces `0 <= reserved <= on_hand`, so a bug in application code fails loudly
instead of overselling.

**One lock order everywhere.** Operations that touch a reservation lock the reservation
row first, then product rows sorted by id. Two requests can wait on each other but cannot
deadlock.

**All-or-nothing carts.** A multi-line reservation either holds every line or none. The
error lists every short line so the storefront can update the cart in one round trip.

**TTL is checked twice.** Confirm refuses a reservation whose `expires_at` has passed even
if the sweeper has not run yet, so expiry is correct without depending on sweeper timing.
The sweeper only returns the stock.

**Sweeper with `SKIP LOCKED`.** Several sweepers can run at once without blocking each
other or a shopper who is confirming at that moment. It works in batches so a backlog
does not become one huge transaction. A plain loop (`stockhold expire --every 30`) is
enough here; the same `expire_stale` function can be called from a Celery beat task if
the platform already runs Celery.

**Idempotency stored with the order.** The `idempotency_keys` row is written in the same
transaction as the order, so there is no window where one exists without the other. The
key is checked again after the reservation lock is taken, which covers two identical
requests arriving at the same moment.

**SQLite fallback.** SQLite ignores `FOR UPDATE`, so on SQLite every write transaction
runs under a process-wide lock. That keeps tests fast and honest about concurrency in a
single process. Production runs on PostgreSQL.

**What is deliberately left out.** No message broker, no cache in front of stock, no
event sourcing, no auth. Each is a reasonable addition for a real deployment, and each
would hide the core mechanism this project exists to show.

## Running tests

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```

The suite runs on SQLite by default. To run it against PostgreSQL (the test database is
dropped and recreated, so point it at a throwaway database):

```bash
docker compose up -d db
STOCKHOLD_TEST_DATABASE_URL=postgresql+psycopg://stockhold:stockhold@localhost:5432/stockhold \
  pytest
```

The concurrency tests start real threads: 40 shoppers competing for 5 units must produce
exactly 5 reservations, and 16 identical confirm requests must produce one order.

## License

MIT. Copyright (c) 2026 Ahmed Mahdy.
