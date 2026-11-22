import logging
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("order_service")
OrderStatus = Literal["pending", "out_for_delivery", "delivered", "lost"]


class Order(BaseModel):
    id: str
    status: OrderStatus
    amount: float
    promised_delivery_at: datetime
    delivered_at: datetime | None = None


class OrderCreate(BaseModel):
    amount: float = Field(gt=0)
    promised_delivery_at: datetime


class OrderStatusUpdate(BaseModel):
    status: OrderStatus
    delivered_at: datetime | None = None


def build_demo_orders() -> dict[str, Order]:
    """Create demo data relative to startup so its meaning does not expire."""
    now = datetime.now(timezone.utc)
    return {
        "order-late": Order(
            id="order-late",
            status="delivered",
            amount=24.50,
            promised_delivery_at=now - timedelta(hours=2),
            delivered_at=now - timedelta(hours=1),
        ),
        "order-on-time": Order(
            id="order-on-time",
            status="delivered",
            amount=18.00,
            promised_delivery_at=now - timedelta(hours=1),
            delivered_at=now - timedelta(hours=2),
        ),
    }


app = FastAPI(title="Order Service")
orders = build_demo_orders()

ALLOWED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    "pending": {"out_for_delivery", "lost"},
    "out_for_delivery": {"delivered", "lost"},
}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/orders/{order_id}", response_model=Order)
async def get_order(order_id: str) -> Order:
    order = orders.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@app.post("/orders", response_model=Order, status_code=201)
async def create_order(request: OrderCreate) -> Order:
    order = Order(
        id=f"sim-{uuid4().hex[:8]}",
        status="pending",
        amount=request.amount,
        promised_delivery_at=request.promised_delivery_at,
    )
    orders[order.id] = order
    logger.info(
        "order_created order_id=%s status=%s promised_delivery_at=%s",
        order.id,
        order.status,
        order.promised_delivery_at.isoformat(),
    )
    return order


@app.patch("/orders/{order_id}/status", response_model=Order)
async def update_order_status(order_id: str, request: OrderStatusUpdate) -> Order:
    order = orders.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")

    allowed_statuses = ALLOWED_TRANSITIONS.get(order.status, set())
    if request.status not in allowed_statuses:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot transition order from {order.status} to {request.status}",
        )

    previous_status = order.status
    order.status = request.status
    if request.status == "delivered":
        order.delivered_at = request.delivered_at or datetime.now(timezone.utc)

    logger.info(
        "order_status_changed order_id=%s from_status=%s to_status=%s",
        order.id,
        previous_status,
        order.status,
    )
    return order
