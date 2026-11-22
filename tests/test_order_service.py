from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from order_service import app, build_demo_orders, orders


client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_orders() -> None:
    orders.clear()
    orders.update(build_demo_orders())
    yield
    orders.clear()
    orders.update(build_demo_orders())


def test_get_seeded_order() -> None:
    response = client.get("/orders/order-late")

    assert response.status_code == 200
    assert response.json()["id"] == "order-late"
    assert response.json()["status"] == "delivered"


def test_unknown_order_returns_404() -> None:
    response = client.get("/orders/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"detail": "Order not found"}


def test_create_and_advance_order_status() -> None:
    promised_at = datetime.now(timezone.utc) + timedelta(seconds=10)
    created = client.post(
        "/orders",
        json={"amount": 12.5, "promised_delivery_at": promised_at.isoformat()},
    )

    assert created.status_code == 201
    order_id = created.json()["id"]
    assert created.json()["status"] == "pending"

    out_for_delivery = client.patch(
        f"/orders/{order_id}/status",
        json={"status": "out_for_delivery"},
    )
    assert out_for_delivery.status_code == 200
    assert out_for_delivery.json()["status"] == "out_for_delivery"

    delivered_at = datetime.now(timezone.utc).isoformat()
    delivered = client.patch(
        f"/orders/{order_id}/status",
        json={"status": "delivered", "delivered_at": delivered_at},
    )
    assert delivered.status_code == 200
    assert delivered.json()["status"] == "delivered"
    assert delivered.json()["delivered_at"] is not None


def test_order_status_cannot_skip_a_state() -> None:
    created = client.post(
        "/orders",
        json={
            "amount": 12.5,
            "promised_delivery_at": (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat(),
        },
    )
    order_id = created.json()["id"]

    response = client.patch(
        f"/orders/{order_id}/status",
        json={"status": "delivered"},
    )

    assert response.status_code == 409
    assert "Cannot transition order" in response.json()["detail"]


def test_order_can_be_marked_lost() -> None:
    created = client.post(
        "/orders",
        json={
            "amount": 12.5,
            "promised_delivery_at": (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat(),
        },
    )
    order_id = created.json()["id"]

    response = client.patch(
        f"/orders/{order_id}/status",
        json={"status": "lost"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "lost"
    assert response.json()["delivered_at"] is None
