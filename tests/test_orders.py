from __future__ import annotations


def _confirm(client, reservation_id: str, key: str | None = "key-1"):
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post(f"/reservations/{reservation_id}/confirm", headers=headers)


def test_confirm_turns_reservation_into_order(client, make_product, reserve):
    product = make_product(on_hand=10)
    reservation = reserve([(product["id"], 3)]).json()

    response = _confirm(client, reservation["id"])

    assert response.status_code == 201
    order = response.json()
    assert order["reservation_id"] == reservation["id"]
    assert order["items"] == [{"product_id": product["id"], "quantity": 3}]
    stock = client.get(f"/products/{product['id']}").json()
    assert (stock["on_hand"], stock["reserved"], stock["available"]) == (7, 0, 7)
    assert client.get(f"/reservations/{reservation['id']}").json()["status"] == "confirmed"


def test_retry_with_same_key_replays_the_original_order(client, make_product, reserve):
    product = make_product(on_hand=10)
    reservation = reserve([(product["id"], 2)]).json()

    first = _confirm(client, reservation["id"], key="checkout-abc")
    retry = _confirm(client, reservation["id"], key="checkout-abc")

    assert first.status_code == 201
    assert retry.status_code == 200
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert retry.json() == first.json()
    # Stock was only taken once.
    assert client.get(f"/products/{product['id']}").json()["on_hand"] == 8


def test_reusing_a_key_for_another_reservation_is_rejected(client, make_product, reserve):
    product = make_product(on_hand=10)
    first = reserve([(product["id"], 1)], cart_id="a").json()
    second = reserve([(product["id"], 1)], cart_id="b").json()
    assert _confirm(client, first["id"], key="shared").status_code == 201

    response = _confirm(client, second["id"], key="shared")

    assert response.status_code == 422
    assert response.json()["error"] == "idempotency_key_reused"
    assert client.get(f"/reservations/{second['id']}").json()["status"] == "active"


def test_confirm_requires_idempotency_key(client, make_product, reserve):
    product = make_product()
    reservation = reserve([(product["id"], 1)]).json()

    assert _confirm(client, reservation["id"], key=None).status_code == 400
    assert _confirm(client, reservation["id"], key="   ").status_code == 400


def test_confirming_twice_with_a_new_key_is_a_conflict(client, make_product, reserve):
    product = make_product()
    reservation = reserve([(product["id"], 1)]).json()
    assert _confirm(client, reservation["id"], key="k1").status_code == 201

    response = _confirm(client, reservation["id"], key="k2")

    assert response.status_code == 409
    assert response.json()["error"] == "invalid_state"


def test_confirming_after_ttl_fails_even_before_the_sweeper_runs(
    client, make_product, reserve, clock
):
    product = make_product(on_hand=2)
    reservation = reserve([(product["id"], 2)], ttl=60).json()
    clock.advance(61)

    response = _confirm(client, reservation["id"])

    assert response.status_code == 410
    assert response.json()["error"] == "reservation_expired"
    assert client.get(f"/products/{product['id']}").json()["on_hand"] == 2


def test_confirmed_reservation_cannot_be_released(client, make_product, reserve):
    product = make_product()
    reservation = reserve([(product["id"], 1)]).json()
    _confirm(client, reservation["id"])

    response = client.post(f"/reservations/{reservation['id']}/release")

    assert response.status_code == 409
