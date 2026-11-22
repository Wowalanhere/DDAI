import logging
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("refund_service")

RefundPercentage = Literal[25, 50, 100]
RefundReason = Literal["lost", "late_under_60", "late_60_to_120", "late_over_120"]


class RefundRequest(BaseModel):
    order_id: str
    order_amount: float = Field(gt=0)
    percentage: RefundPercentage
    reason: RefundReason


class Refund(BaseModel):
    order_id: str
    amount: float
    percentage: RefundPercentage
    reason: RefundReason
    status: Literal["completed"] = "completed"
    issued_at: datetime


class RefundResult(Refund):
    created: bool


app = FastAPI(title="Refund Service")
refunds: dict[str, Refund] = {}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/refunds", response_model=RefundResult)
async def create_refund(
    request: RefundRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> RefundResult:
    if idempotency_key != request.order_id:
        raise HTTPException(status_code=400, detail="Idempotency-Key must match order_id")

    existing = refunds.get(idempotency_key)
    if existing is not None:
        logger.info(
            "refund_reused order_id=%s idempotency_key=%s",
            request.order_id,
            idempotency_key,
        )
        return RefundResult(**existing.model_dump(), created=False)

    refund = Refund(
        order_id=request.order_id,
        amount=round(request.order_amount * request.percentage / 100, 2),
        percentage=request.percentage,
        reason=request.reason,
        issued_at=datetime.now(timezone.utc),
    )
    refunds[idempotency_key] = refund
    logger.info(
        "refund_created order_id=%s idempotency_key=%s percentage=%s amount=%.2f",
        request.order_id,
        idempotency_key,
        refund.percentage,
        refund.amount,
    )
    return RefundResult(**refund.model_dump(), created=True)


@app.get("/refunds/{order_id}", response_model=Refund)
async def get_refund(order_id: str) -> Refund:
    refund = refunds.get(order_id)
    if refund is None:
        raise HTTPException(status_code=404, detail="Refund not found")
    return refund


@app.get("/refunds", response_model=list[Refund])
async def list_refunds() -> list[Refund]:
    return list(refunds.values())
