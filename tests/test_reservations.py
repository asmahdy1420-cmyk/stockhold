from __future__ import annotations


def _product(client, product_id: int) -> dict:
    return client.get(f"/products/{product_id}").json()


def test_reserve_holds_stock_with_ttl(client, make_product, reserve, clock):
    product = make_product(on_hand=10)

    response = reserve([(product["id"], 3)], ttl=120)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "active"
    assert body["items"] == [{"product_id": product["id"], "quantity": 3}]
    assert body["expires_at"].startswith("2026-01-01T12:02:00")
    assert _product(client, product["id"])["available"] == 7


def test_duplicate_lines_are_merged(client, make_product, reserve):
    product = make_product(on_hand=10)

    body = reserve([(product["id"], 2), (product["id"], 3)]).json()

    assert body["items"] == [{"product_id": product["id"], "quantity": 5}]
    assert _product(client, product["id"])["reserved"] == 5


def test_multi_line_reservation_is_all_or_nothing(client, make_product, reserve):
    plenty = make_product(on_hand=10)
    scarce = make_product(on_hand=1)

    response = reserve([(plenty["id"], 2), (scarce["id"], 2)])

    assert response.status_code == 409
    error = response.json()
    assert error["error"] == "insufficient_stock"
    assert error["details"]["lines"] == [
        {"product_id": scarce["id"], "requested": 2, "available": 1}
    ]
    # The line that could have been satisfied was not reserved either.
    assert _product(client, plenty["id"])["reserved"] == 0


def test_reserving_unknown_product_returns_404(client, reserve):
    response = reserve([(12345, 1)])

    assert response.status_code == 404
    assert response.json()["details"]["product_ids"] == [12345]


def test_ttl_above_maximum_is_rejected(client, make_product, reserve):
    product = make_product()

    response = reserve([(product["id"], 1)], ttl=10_000)

    assert response.status_code == 400


def test_release_returns_stock_and_is_repeatable(client, make_product, reserve):
    product = make_product(on_hand=4)
    reservation = reserve([(product["id"], 4)]).json()

    first = client.post(f"/reservations/{reservation['id']}/release")
    second = client.post(f"/reservations/{reservation['id']}/release")

    assert first.status_code == 200
    assert first.json()["status"] == "released"
    assert second.status_code == 200
    assert _product(client, product["id"])["available"] == 4


def test_expired_reservations_are_swept(client, make_product, reserve, clock):
    product = make_product(on_hand=3)
    stale = reserve([(product["id"], 2)], cart_id="old", ttl=60).json()
    clock.advance(30)
    fresh = reserve([(product["id"], 1)], cart_id="new", ttl=60).json()
    clock.advance(45)  # first reservation is 75s old, second 45s old

    swept = client.post("/admin/expire")

    assert swept.json() == {"expired": 1}
    assert client.get(f"/reservations/{stale['id']}").json()["status"] == "expired"
    assert client.get(f"/reservations/{fresh['id']}").json()["status"] == "active"
    assert _product(client, product["id"])["reserved"] == 1
