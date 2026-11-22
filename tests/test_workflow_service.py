import json
import logging
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

import workflow_service
from workflow_service import app, get_order_client, get_refund_client


Handler = Callable[[httpx.Request], httpx.Response]


def order_payload(
    order_id: str,
    *,
    minutes_late: float,
    status: str = "delivered",
) -> dict[str, object]:
    promised_at = (
        datetime(2099, 1, 1, 12, tzinfo=timezone.utc)
        if status == "lost"
        else datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    )
    return {
        "id": order_id,
        "status": status,
        "amount": 24.5,
        "promised_delivery_at": promised_at.isoformat(),
        "delivered_at": (
            (promised_at + timedelta(minutes=minutes_late)).isoformat()
            if status == "delivered"
            else None
        ),
    }


def successful_refund(request: httpx.Request, *, created: bool = True) -> httpx.Response:
    payload = json.loads(request.content)
    assert request.headers["Idempotency-Key"] == payload["order_id"]
    return httpx.Response(
        200,
        json={
            "order_id": payload["order_id"],
            "amount": round(payload["order_amount"] * payload["percentage"] / 100, 2),
            "percentage": payload["percentage"],
            "reason": payload["reason"],
            "status": "completed",
            "issued_at": "2026-01-01T14:00:00Z",
            "created": created,
        },
    )


def configure_transports(
    order_handler: Handler,
    refund_handler: Handler = successful_refund,
) -> TestClient:
    async def override_order_client() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(order_handler),
            base_url="http://order-service",
        ) as client:
            yield client

    async def override_refund_client() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(refund_handler),
            base_url="http://refund-service",
        ) as client:
            yield client

    app.dependency_overrides[get_order_client] = override_order_client
    app.dependency_overrides[get_refund_client] = override_refund_client
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_overrides() -> None:
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


def test_late_order_calls_refund_service_and_logs_transitions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def order_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=order_payload("order-late", minutes_late=121))

    client = configure_transports(order_handler)
    with caplog.at_level(logging.INFO, logger="refund_workflow"):
        response = client.post("/workflows/execute", json={"order_id": "order-late"})

    assert response.status_code == 200
    assert response.json()["outcome"] == "refunded"
    assert response.json()["refund"]["amount"] == 24.5
    assert response.json()["refund"]["percentage"] == 100
    assert response.json()["refund"]["status"] == "completed"
    assert "from_state=START to_state=CHECK_LOST" in caplog.text
    assert "from_state=CHECK_LOST to_state=CHECK_LATE" in caplog.text
    assert "from_state=CHECK_LATE to_state=ISSUE_REFUND" in caplog.text
    assert "from_state=ISSUE_REFUND to_state=END" in caplog.text


def test_on_time_order_does_not_call_refund_service() -> None:
    def order_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=order_payload("order-on-time", minutes_late=-60))

    def refund_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Refund service should not be called")

    client = configure_transports(order_handler, refund_handler)
    response = client.post("/workflows/execute", json={"order_id": "order-on-time"})

    assert response.status_code == 200
    assert response.json() == {
        "order_id": "order-on-time",
        "is_late": False,
        "outcome": "not_late",
        "refund": None,
    }


def test_order_id_is_idempotency_key_and_existing_refund_is_reported() -> None:
    calls = 0

    def order_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=order_payload("order-late", minutes_late=121))

    def refund_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["Idempotency-Key"] == "order-late"
        return successful_refund(request, created=calls == 1)

    client = configure_transports(order_handler, refund_handler)
    first = client.post("/workflows/execute", json={"order_id": "order-late"})
    second = client.post("/workflows/execute", json={"order_id": "order-late"})

    assert first.json()["outcome"] == "refunded"
    assert second.json()["outcome"] == "already_refunded"
    assert calls == 2


@pytest.mark.parametrize(
    ("minutes_late", "expected_percentage", "expected_amount"),
    [(30, 25, 6.12), (60, 50, 12.25), (120, 50, 12.25), (121, 100, 24.5)],
)
def test_partial_refund_tiers(
    minutes_late: float,
    expected_percentage: int,
    expected_amount: float,
) -> None:
    def order_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=order_payload("tier-order", minutes_late=minutes_late))

    client = configure_transports(order_handler)
    response = client.post("/workflows/execute", json={"order_id": "tier-order"})

    assert response.json()["refund"]["percentage"] == expected_percentage
    assert response.json()["refund"]["amount"] == expected_amount


def test_lost_order_receives_full_refund_even_when_not_late() -> None:
    def order_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=order_payload("lost-order", minutes_late=-60, status="lost"),
        )

    client = configure_transports(order_handler)
    response = client.post("/workflows/execute", json={"order_id": "lost-order"})

    assert response.json()["is_late"] is False
    assert response.json()["refund"]["percentage"] == 100
    assert response.json()["refund"]["reason"] == "lost"


def test_refund_call_retries_transient_failures_with_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleep = AsyncMock()
    monkeypatch.setattr(workflow_service.asyncio, "sleep", sleep)
    monkeypatch.setattr(workflow_service.random, "uniform", lambda _start, _end: 0.0)

    def order_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=order_payload("retry-order", minutes_late=121))

    def refund_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("slow refund service", request=request)
        if calls == 2:
            return httpx.Response(503, json={"detail": "busy"})
        return successful_refund(request)

    client = configure_transports(order_handler, refund_handler)
    response = client.post("/workflows/execute", json={"order_id": "retry-order"})

    assert response.status_code == 200
    assert calls == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.1, 0.2]


def test_refund_call_stops_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    monkeypatch.setattr(workflow_service.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(workflow_service.random, "uniform", lambda _start, _end: 0.0)

    def order_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=order_payload("failed-order", minutes_late=121))

    def refund_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"detail": "busy"})

    client = configure_transports(order_handler, refund_handler)
    response = client.post("/workflows/execute", json={"order_id": "failed-order"})

    assert response.status_code == 502
    assert response.json() == {"detail": "Refund service unavailable"}
    assert calls == 3


def test_unknown_order_is_propagated_as_404() -> None:
    def order_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Order not found"})

    client = configure_transports(order_handler)
    response = client.post("/workflows/execute", json={"order_id": "missing"})
    assert response.status_code == 404


def test_order_service_connection_failure_returns_502() -> None:
    def order_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = configure_transports(order_handler)
    response = client.post("/workflows/execute", json={"order_id": "order-late"})
    assert response.status_code == 502
