import pytest
from fastapi.testclient import TestClient

from refund_service import app, refunds


client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_refunds() -> None:
    refunds.clear()
    yield
    refunds.clear()


def refund_request(order_id: str = "order-1") -> dict[str, object]:
    return {
        "order_id": order_id,
        "order_amount": 20.0,
        "percentage": 50,
        "reason": "late_60_to_120",
    }


def test_create_refund_is_idempotent_by_order_id() -> None:
    headers = {"Idempotency-Key": "order-1"}
    first = client.post("/refunds", headers=headers, json=refund_request())
    second = client.post("/refunds", headers=headers, json=refund_request())

    assert first.status_code == 200
    assert first.json()["created"] is True
    assert first.json()["amount"] == 10.0
    assert first.json()["status"] == "completed"
    assert second.json()["created"] is False
    assert second.json()["issued_at"] == first.json()["issued_at"]
    assert len(refunds) == 1


def test_idempotency_key_must_match_order_id() -> None:
    response = client.post(
        "/refunds",
        headers={"Idempotency-Key": "different-order"},
        json=refund_request(),
    )

    assert response.status_code == 400
    assert refunds == {}


def test_admin_can_get_refund_and_list_all_refunds() -> None:
    for order_id in ("order-1", "order-2"):
        client.post(
            "/refunds",
            headers={"Idempotency-Key": order_id},
            json=refund_request(order_id),
        )

    one = client.get("/refunds/order-1")
    all_refunds = client.get("/refunds")

    assert one.status_code == 200
    assert one.json()["order_id"] == "order-1"
    assert all_refunds.status_code == 200
    assert {refund["order_id"] for refund in all_refunds.json()} == {"order-1", "order-2"}


def test_unknown_refund_returns_404() -> None:
    response = client.get("/refunds/missing")
    assert response.status_code == 404
