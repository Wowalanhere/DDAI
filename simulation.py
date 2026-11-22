"""Launch both services and continuously simulate orders and refund workflows."""

import argparse
import asyncio
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx


ORDER_PORT = os.getenv("ORDER_SERVICE_PORT", "8000")
WORKFLOW_PORT = os.getenv("WORKFLOW_SERVICE_PORT", "8001")
REFUND_PORT = os.getenv("REFUND_SERVICE_PORT", "8002")
ORDER_URL = f"http://127.0.0.1:{ORDER_PORT}"
WORKFLOW_URL = f"http://127.0.0.1:{WORKFLOW_PORT}"
REFUND_URL = f"http://127.0.0.1:{REFUND_PORT}"
logger = logging.getLogger("simulation")


@dataclass
class TrackedOrder:
    id: str
    created_at: float
    status: str = "pending"
    should_be_lost: bool = False


def start_services() -> list[subprocess.Popen[bytes]]:
    workflow_environment = os.environ.copy()
    workflow_environment["ORDER_SERVICE_URL"] = ORDER_URL
    workflow_environment["REFUND_SERVICE_URL"] = REFUND_URL
    commands = [
        ([sys.executable, "-m", "uvicorn", "order_service:app", "--port", ORDER_PORT], None),
        (
            [
                sys.executable,
                "-m",
                "uvicorn",
                "workflow_service:app",
                "--port",
                WORKFLOW_PORT,
            ],
            workflow_environment,
        ),
        ([sys.executable, "-m", "uvicorn", "refund_service:app", "--port", REFUND_PORT], None),
    ]
    return [subprocess.Popen(command, env=environment) for command, environment in commands]


def stop_services(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


async def wait_until_ready(
    client: httpx.AsyncClient,
    processes: list[subprocess.Popen[bytes]],
) -> None:
    for _ in range(50):
        if any(process.poll() is not None for process in processes):
            raise RuntimeError(
                "A service exited during startup. Check whether ports "
                f"{ORDER_PORT}, {WORKFLOW_PORT}, and {REFUND_PORT} are already in use."
            )
        try:
            order_response, workflow_response, refund_response = await asyncio.gather(
                client.get(f"{ORDER_URL}/health"),
                client.get(f"{WORKFLOW_URL}/health"),
                client.get(f"{REFUND_URL}/health"),
            )
            if (
                order_response.is_success
                and workflow_response.is_success
                and refund_response.is_success
            ):
                return
        except httpx.RequestError:
            pass
        await asyncio.sleep(0.1)
    raise RuntimeError("Services did not become ready within 5 seconds")


async def create_order(
    client: httpx.AsyncClient,
    tracked_orders: list[TrackedOrder],
    sequence: int,
) -> None:
    profiles = [
        (-30 * 60, False, "late_under_60"),
        (-90 * 60, False, "late_60_to_120"),
        (-180 * 60, False, "late_over_120"),
        (10, False, "on_time"),
        (10, True, "lost"),
    ]
    promised_seconds, should_be_lost, profile = profiles[(sequence - 1) % len(profiles)]
    response = await client.post(
        f"{ORDER_URL}/orders",
        json={
            "amount": round(10 + sequence * 0.75, 2),
            "promised_delivery_at": (
                datetime.now(timezone.utc) + timedelta(seconds=promised_seconds)
            ).isoformat(),
        },
    )
    response.raise_for_status()
    order = response.json()
    tracked_orders.append(
        TrackedOrder(
            id=order["id"],
            created_at=asyncio.get_running_loop().time(),
            should_be_lost=should_be_lost,
        )
    )
    logger.info(
        "simulation_created order_id=%s profile=%s promise_offset_seconds=%s",
        order["id"],
        profile,
        promised_seconds,
    )


async def advance_orders(
    client: httpx.AsyncClient,
    tracked_orders: list[TrackedOrder],
) -> None:
    now = asyncio.get_running_loop().time()
    for tracked in tracked_orders:
        age = now - tracked.created_at
        new_status: str | None = None
        if tracked.status == "pending" and age >= 1:
            new_status = "out_for_delivery"
        elif tracked.status == "out_for_delivery" and age >= 4:
            new_status = "lost" if tracked.should_be_lost else "delivered"

        if new_status is None:
            continue

        payload: dict[str, str] = {"status": new_status}
        if new_status == "delivered":
            payload["delivered_at"] = datetime.now(timezone.utc).isoformat()
        response = await client.patch(
            f"{ORDER_URL}/orders/{tracked.id}/status",
            json=payload,
        )
        response.raise_for_status()
        logger.info(
            "simulation_status order_id=%s from_status=%s to_status=%s",
            tracked.id,
            tracked.status,
            new_status,
        )
        tracked.status = new_status


async def order_loop(client: httpx.AsyncClient, tracked_orders: list[TrackedOrder]) -> None:
    sequence = 1
    while True:
        await advance_orders(client, tracked_orders)
        await create_order(client, tracked_orders, sequence)
        sequence += 1
        await asyncio.sleep(1)


async def workflow_loop(client: httpx.AsyncClient, tracked_orders: list[TrackedOrder]) -> None:
    while True:
        await asyncio.sleep(5)
        # Bound each cycle while retaining repeat checks that demonstrate idempotency.
        for tracked in list(tracked_orders[-10:]):
            response = await client.post(
                f"{WORKFLOW_URL}/workflows/execute",
                json={"order_id": tracked.id},
            )
            response.raise_for_status()
            result = response.json()
            logger.info(
                "simulation_workflow order_id=%s order_status=%s is_late=%s outcome=%s",
                tracked.id,
                tracked.status,
                result["is_late"],
                result["outcome"],
            )


async def run_simulation(
    duration: float | None,
    processes: list[subprocess.Popen[bytes]],
) -> None:
    tracked_orders: list[TrackedOrder] = []
    async with httpx.AsyncClient(timeout=5.0) as client:
        await wait_until_ready(client, processes)
        logger.info("simulation_started order_interval=1s workflow_interval=5s")
        tasks = [
            asyncio.create_task(order_loop(client, tracked_orders)),
            asyncio.create_task(workflow_loop(client, tracked_orders)),
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=duration)
        except TimeoutError:
            logger.info("simulation_stopped reason=duration_elapsed")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Stop after this many seconds; by default run until Ctrl+C.",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    processes = start_services()
    try:
        asyncio.run(run_simulation(args.duration, processes))
    except KeyboardInterrupt:
        logger.info("simulation_stopped reason=keyboard_interrupt")
    finally:
        stop_services(processes)


if __name__ == "__main__":
    main()
