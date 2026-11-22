# Delayed Delivery Refund Workflow

A minimal three-service FastAPI example with an automatic simulation.

```text
Order service :8000              Workflow service :8001             Refund service :8002
create/update/get -- HTTPX --> START
                               CHECK_LOST
                                |-- lost ---------> ISSUE_REFUND -- HTTPX --> create/reuse refund
                                \-- CHECK_LATE                            (order_id idempotency key)
                                     |-- >120 min --> 100% --/
                                     |-- 60-120  -->  50% --/
                                     |-- 0-60    -->  25% --/
                                     \-- on time -----------> END
```

## Approach and design decisions

- The **order service** owns all order state in memory. Normal delivery advances through `pending -> out_for_delivery -> delivered`; an order can instead move from `pending` or `out_for_delivery` to the terminal `lost` status.
- The **workflow service** fetches order state, applies the DAG rules, and calls the refund service when payment is required. It stores no refund status.
- The **refund service** owns all in-memory refund records on port `8002`. Its POST endpoint uses the order ID from the `Idempotency-Key` header, so a retry or repeated workflow run returns the original refund instead of paying twice.
- Lateness uses timezone-aware UTC timestamps. A delivered order is late when `delivered_at` is after `promised_delivery_at`; an undelivered order is late once its promised time has passed.
- Refund rules are applied in order: a lost order always receives 100%; otherwise more than 120 minutes late receives 100%, 60 through 120 minutes inclusive receives 50%, and a positive delay under 60 minutes receives 25%. Refund amounts are rounded to two decimal places.
- Idempotency preserves the first issued refund. A later workflow check reports `already_refunded` rather than changing or duplicating that refund.
- Refund calls use a one-second HTTP timeout and at most three attempts. Timeouts, connection failures, and HTTP 5xx responses retry with exponential backoff (about 100 ms, then 200 ms) plus random jitter. HTTP 4xx and invalid responses fail immediately.
- The **simulation is an external client**, not background logic hidden inside either service. This keeps service ownership clear and exercises the real HTTP APIs.
- The simulator creates one order each second and advances existing orders each second. It cycles through under-60-minute, 60-to-120-minute, over-120-minute, on-time, and lost profiles so every refund branch appears without a long wait.
- Every five seconds, the simulator sends the ten most recently created order IDs to the workflow service. The bounded window prevents request volume from growing forever and repeated IDs demonstrate idempotent `already_refunded` results.
- Every workflow state transition and refund decision is logged with its order ID. Order creation and status changes are logged too.
- All state is process-local and resets whenever the services restart.

## Setup

Python 3.10 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run the automatic simulation

One command launches all three Uvicorn services, waits for them to become healthy, and starts the simulation:

```bash
python simulation.py
```

Press `Ctrl+C` to stop the simulator and all services. For a finite run, pass a duration in seconds:

```bash
python simulation.py --duration 15
```

The console output shows order creation, status changes, workflow state transitions, late decisions, and refund outcomes.

Ports `8000`, `8001`, and `8002` must be available. To use different ports, set the simulation variables; the launcher automatically gives both downstream URLs to the workflow service:

```bash
ORDER_SERVICE_PORT=18000 WORKFLOW_SERVICE_PORT=18001 REFUND_SERVICE_PORT=18002 \
  python simulation.py --duration 15
```

## Run the services manually

Start the order service:

```bash
uvicorn order_service:app --host 127.0.0.1 --port 8000
```

Start the workflow service in another terminal:

```bash
uvicorn workflow_service:app --host 127.0.0.1 --port 8001
```

Start the refund service in a third terminal:

```bash
uvicorn refund_service:app --host 127.0.0.1 --port 8002
```

Set `ORDER_SERVICE_URL` or `REFUND_SERVICE_URL` to override the workflow service's default downstream URLs.

## API examples

Create an order:

```bash
curl -sS -X POST http://127.0.0.1:8000/orders \
  -H 'content-type: application/json' \
  -d '{"amount":24.50,"promised_delivery_at":"2030-01-01T12:00:00Z"}'
```

Read an order:

```bash
curl -sS http://127.0.0.1:8000/orders/order-late
```

Advance a created order by replacing `<order-id>` with the ID returned above:

```bash
curl -sS -X PATCH http://127.0.0.1:8000/orders/<order-id>/status \
  -H 'content-type: application/json' \
  -d '{"status":"out_for_delivery"}'
```

Mark an active order as lost:

```bash
curl -sS -X PATCH http://127.0.0.1:8000/orders/<order-id>/status \
  -H 'content-type: application/json' \
  -d '{"status":"lost"}'
```

Execute a refund workflow:

```bash
curl -sS -X POST http://127.0.0.1:8001/workflows/execute \
  -H 'content-type: application/json' \
  -d '{"order_id":"order-late"}'
```

Admin: check one order's refund status:

```bash
curl -sS http://127.0.0.1:8002/refunds/order-late
```

Admin: list all refund statuses:

```bash
curl -sS http://127.0.0.1:8002/refunds
```

## Tests

Tests use FastAPI's test client and HTTPX mock transport, so no servers need to be running:

```bash
pytest -q
```

This demonstration intentionally excludes persistent storage, authentication, a payment provider, distributed locks, and production orchestration.

## Prompt and response

<details>
<summary>Prompt sent by the user</summary>

> Summarize everything we worked on in this session: the problem we were solving, the key design decisions and why we made them, any tradeoffs or things we'd do differently with more time, and the current state of the code (what works, what's incomplete or untested). Then draw an ASCII architecture diagram of the system we built — services/components as boxes, arrows for the data flow and API calls between them, and labels for the protocol (HTTP, in-memory, etc.) on each connection.

</details>

<details>
<summary>Response sent by the assistant</summary>

### Problem

We built a minimal delayed-delivery refund system in Python using FastAPI, Uvicorn, and HTTPX. It keeps service state in memory, evaluates late-delivery refund rules as a DAG, supports lost orders, issues partial or full refunds, prevents duplicates, retries temporary refund failures, runs an automatic simulation, and exposes admin refund APIs.

### Key design decisions

- **Three service boundaries:** the order service on port `8000` owns orders, the workflow service on port `8001` evaluates the DAG without storing refunds, and the refund service on port `8002` owns refunds. Port `8001` replaced the originally mentioned port `800` because low-numbered ports can require elevated privileges.
- **Order lifecycle:** orders follow `pending -> out_for_delivery -> delivered`, with `lost` available as a terminal transition from either active state.
- **Refund rules:** lost orders receive 100%; delays over 120 minutes receive 100%; delays from 60 through 120 minutes receive 50%; positive delays under 60 minutes receive 25%; on-time orders receive no refund.
- **Idempotency:** the workflow sends `order_id` in the `Idempotency-Key` header. The refund service stores and returns the first refund for that key, protecting against duplicate workflow runs and ambiguous retry outcomes.
- **Retries:** refund calls have a one-second timeout and at most three attempts. Timeouts, connection errors, and HTTP 5xx responses use exponential backoff with jitter; HTTP 4xx and invalid responses fail immediately.
- **Simulation:** the simulator starts all three services, creates and advances orders every second, and runs workflows every five seconds. Its profiles exercise every refund tier, on-time delivery, lost orders, and repeated idempotent checks.

### Tradeoffs and future improvements

- State is in memory and disappears on restart.
- Idempotency is process-local; multiple workers would need shared storage and a unique database constraint.
- Refunds are synchronous and only have a `completed` state.
- There is no real payment provider, authentication, pagination, metrics, tracing, or durable audit history.
- The workflow retries refund-service calls but not order-service calls.
- Retry settings are constants rather than runtime configuration.
- The first refund is immutable. A production system may need top-up refunds if an order later enters a higher tier or becomes lost.
- Retry behavior is unit-tested with simulated failures but has not been load-tested against real network degradation.

### Current state

The three FastAPI services, partial-refund boundaries, lost-order handling, idempotency, retry behavior, admin endpoints, health endpoints, automatic simulation, curl examples, and logging are implemented. The suite has 21 passing tests, and a live three-service simulation verified the order-to-workflow-to-refund HTTP path. Multi-worker concurrency, process-crash recovery, persistent storage, and real payment integration remain incomplete or untested.

### Architecture

```text
┌───────────────────────────────┐
│ Automatic Simulator           │
│ simulation.py                 │
│                               │
│ • Starts all three services   │
│ • Creates orders every 1 sec  │
│ • Changes statuses every 1 sec│
│ • Triggers DAG every 5 sec    │
└───────────┬───────────────────┘
            │ HTTP/JSON
            │ POST /orders
            │ PATCH /orders/{id}/status
            ▼
┌───────────────────────────────┐
│ Order Service                 │
│ 127.0.0.1:8000                │
│ In-memory order dictionary    │
│                               │
│ GET   /orders/{order_id}      │
│ POST  /orders                 │
│ PATCH /orders/{id}/status     │
└──────────────┬────────────────┘
               ▲
               │ HTTP/JSON
               │ GET /orders/{order_id}
               │
┌──────────────┴────────────────┐       HTTP/JSON
│ Workflow Service              │◀──────────────────── Simulator
│ 127.0.0.1:8001                │       POST /workflows/execute
│ No refund persistence         │
│                               │
│ START -> CHECK_LOST           │
│       -> CHECK_LATE           │
│       -> ISSUE_REFUND -> END  │
└──────────────┬────────────────┘
               │ HTTP/JSON
               │ POST /refunds
               │ Idempotency-Key: order_id
               │ 3 attempts + backoff + jitter
               ▼
┌───────────────────────────────┐
│ Refund Service                │
│ 127.0.0.1:8002                │
│ In-memory refund dictionary   │
│ keyed by order_id             │
│                               │
│ POST /refunds                 │
│ GET  /refunds/{order_id}      │◀──── Admin via HTTP/JSON
│ GET  /refunds                 │◀──── Admin via HTTP/JSON
└───────────────────────────────┘
```

</details>
