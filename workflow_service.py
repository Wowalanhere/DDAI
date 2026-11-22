import asyncio
import logging
import os
import random
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ValidationError


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("refund_workflow")

ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://127.0.0.1:8000")
REFUND_SERVICE_URL = os.getenv("REFUND_SERVICE_URL", "http://127.0.0.1:8002")
MAX_REFUND_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.1

RefundPercentage = Literal[25, 50, 100]
RefundReason = Literal["lost", "late_under_60", "late_60_to_120", "late_over_120"]


class WorkflowRequest(BaseModel):
    order_id: str


class OrderDetails(BaseModel):
    id: str
    status: str
    amount: float
    promised_delivery_at: datetime
    delivered_at: datetime | None = None


class Refund(BaseModel):
    order_id: str
    amount: float
    percentage: RefundPercentage
    reason: RefundReason
    status: Literal["completed"]
    issued_at: datetime


class RefundServiceResult(Refund):
    created: bool


class WorkflowResponse(BaseModel):
    order_id: str
    is_late: bool
    outcome: Literal["refunded", "already_refunded", "not_late"]
    refund: Refund | None = None


app = FastAPI(title="Delayed Delivery Refund Workflow")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def get_order_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=ORDER_SERVICE_URL, timeout=5.0) as client:
        yield client


async def get_refund_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=REFUND_SERVICE_URL, timeout=1.0) as client:
        yield client


OrderClient = Annotated[httpx.AsyncClient, Depends(get_order_client)]
RefundClient = Annotated[httpx.AsyncClient, Depends(get_refund_client)]


def log_transition(order_id: str, previous: str, new: str) -> None:
    logger.info(
        "workflow_transition order_id=%s from_state=%s to_state=%s",
        order_id,
        previous,
        new,
    )


def order_is_late(order: OrderDetails, now: datetime) -> bool:
    comparison_time = order.delivered_at or now
    return comparison_time > order.promised_delivery_at


def late_minutes(order: OrderDetails, now: datetime) -> float:
    comparison_time = order.delivered_at or now
    return max(0.0, (comparison_time - order.promised_delivery_at).total_seconds() / 60)


def refund_rule(order: OrderDetails, now: datetime) -> tuple[int, str, float]:
    minutes = late_minutes(order, now)
    if order.status == "lost":
        return 100, "lost", minutes
    if minutes > 120:
        return 100, "late_over_120", minutes
    if minutes >= 60:
        return 50, "late_60_to_120", minutes
    if minutes > 0:
        return 25, "late_under_60", minutes
    return 0, "not_late", minutes


async def fetch_order(order_id: str, client: httpx.AsyncClient) -> OrderDetails:
    try:
        response = await client.get(f"/orders/{order_id}")
    except httpx.RequestError as exc:
        logger.error("order_service_error order_id=%s error=%s", order_id, exc)
        raise HTTPException(status_code=502, detail="Order service unavailable") from exc

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Order not found")
    if response.is_error:
        logger.error(
            "order_service_error order_id=%s status_code=%s",
            order_id,
            response.status_code,
        )
        raise HTTPException(status_code=502, detail="Order service error")

    try:
        return OrderDetails.model_validate(response.json())
    except (ValueError, ValidationError) as exc:
        logger.error("invalid_order_response order_id=%s", order_id)
        raise HTTPException(status_code=502, detail="Invalid order service response") from exc


async def request_refund(
    order: OrderDetails,
    percentage: int,
    reason: str,
    client: httpx.AsyncClient,
) -> RefundServiceResult:
    last_error = "unknown error"
    for attempt in range(1, MAX_REFUND_ATTEMPTS + 1):
        try:
            response = await client.post(
                "/refunds",
                headers={"Idempotency-Key": order.id},
                json={
                    "order_id": order.id,
                    "order_amount": order.amount,
                    "percentage": percentage,
                    "reason": reason,
                },
            )
            if response.status_code < 500:
                if response.is_error:
                    logger.error(
                        "refund_service_rejected order_id=%s status_code=%s",
                        order.id,
                        response.status_code,
                    )
                    raise HTTPException(status_code=502, detail="Refund service rejected request")
                try:
                    return RefundServiceResult.model_validate(response.json())
                except (ValueError, ValidationError) as exc:
                    raise HTTPException(
                        status_code=502,
                        detail="Invalid refund service response",
                    ) from exc
            last_error = f"HTTP {response.status_code}"
        except httpx.RequestError as exc:
            last_error = str(exc)

        if attempt == MAX_REFUND_ATTEMPTS:
            break

        backoff = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        delay = backoff + random.uniform(0, backoff)
        logger.warning(
            "refund_retry order_id=%s attempt=%s next_attempt=%s delay_seconds=%.3f error=%s",
            order.id,
            attempt,
            attempt + 1,
            delay,
            last_error,
        )
        await asyncio.sleep(delay)

    logger.error(
        "refund_service_failed order_id=%s attempts=%s error=%s",
        order.id,
        MAX_REFUND_ATTEMPTS,
        last_error,
    )
    raise HTTPException(status_code=502, detail="Refund service unavailable")


@app.post("/workflows/execute", response_model=WorkflowResponse)
async def execute_workflow(
    request: WorkflowRequest,
    order_client: OrderClient,
    refund_client: RefundClient,
) -> WorkflowResponse:
    order_id = request.order_id
    log_transition(order_id, "NONE", "START")

    order = await fetch_order(order_id, order_client)
    log_transition(order_id, "START", "CHECK_LOST")
    now = datetime.now(timezone.utc)
    is_lost = order.status == "lost"
    logger.info("lost_decision order_id=%s is_lost=%s", order_id, is_lost)

    previous_state = "CHECK_LOST"
    if not is_lost:
        log_transition(order_id, "CHECK_LOST", "CHECK_LATE")
        previous_state = "CHECK_LATE"

    percentage, reason, minutes = refund_rule(order, now)
    is_late = order_is_late(order, now)
    logger.info(
        "late_decision order_id=%s is_late=%s late_minutes=%.2f refund_percentage=%s",
        order_id,
        is_late,
        minutes,
        percentage,
    )

    if percentage == 0:
        logger.info("refund_decision order_id=%s action=skipped reason=not_late", order_id)
        log_transition(order_id, "CHECK_LATE", "END")
        return WorkflowResponse(order_id=order_id, is_late=False, outcome="not_late")

    log_transition(order_id, previous_state, "ISSUE_REFUND")
    result = await request_refund(order, percentage, reason, refund_client)
    outcome = "refunded" if result.created else "already_refunded"
    logger.info(
        "refund_decision order_id=%s action=%s percentage=%s reason=%s amount=%.2f",
        order_id,
        "issued" if result.created else "already_exists",
        result.percentage,
        result.reason,
        result.amount,
    )

    log_transition(order_id, "ISSUE_REFUND", "END")
    return WorkflowResponse(
        order_id=order_id,
        is_late=is_late,
        outcome=outcome,
        refund=Refund.model_validate(result.model_dump()),
    )
