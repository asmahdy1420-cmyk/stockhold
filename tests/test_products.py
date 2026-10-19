from __future__ import annotations


def test_create_and_fetch_product(client, make_product):
    product = make_product(on_hand=7, sku="MUG-BLUE")

    response = client.get(f"/products/{product['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["sku"] == "MUG-BLUE"
    assert body["on_hand"] == 7
    assert body["reserved"] == 0
    assert body["available"] == 7


def test_duplicate_sku_is_rejected(client, make_product):
    make_product(sku="DUP-1")

    response = client.post("/products", json={"sku": "DUP-1", "name": "Again", "on_hand": 1})

    assert response.status_code == 409
    assert response.json()["error"] == "conflict"


def test_unknown_product_returns_404(client):
    response = client.get("/products/9999")

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_stock_adjustment_cannot_drop_below_reserved(client, make_product, reserve):
    product = make_product(on_hand=5)
    assert reserve([(product["id"], 4)]).status_code == 201

    too_far = client.post(f"/products/{product['id']}/adjustments", json={"delta": -2})
    allowed = client.post(f"/products/{product['id']}/adjustments", json={"delta": -1})

    assert too_far.status_code == 409
    assert too_far.json()["error"] == "insufficient_stock"
    assert allowed.status_code == 200
    assert allowed.json() == {**allowed.json(), "on_hand": 4, "reserved": 4, "available": 0}
