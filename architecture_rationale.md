# Architecture Rationale

This document explains the key architectural decisions made when designing the webhook delivery system.

The design prioritizes:
- **Reliability** — deliveries are persisted and retried
- **Fairness** — no single user can monopolize the pipeline
- **Operational simplicity** — minimal moving parts
- **Observability** — real-time monitoring during testing

---

## Why Redis for Queueing

### 1. Extremely Fast Enqueue / Dequeue

Webhook delivery systems can generate bursts of events. Redis provides:
- `O(1)` push operations
- `O(1)` pop operations
- Very low latency

This makes it ideal for managing high-throughput webhook delivery queues.

### 2. Natural Fit for Per-User Queues

Redis lists allow simple, idiomatic modeling of per-user queues:

```
webhook:user:<user_id>:queue

# Examples:
webhook:user:alice:queue
webhook:user:bob:queue
```

This structure makes fairness scheduling straightforward to implement and reason about.

### 3. Simple Distributed State

Redis is also used to share scheduling state across all workers without complex coordination:

```
webhook:active_users   # set of users with pending deliveries
webhook:rate_limit     # global deliveries-per-second cap
```

---

## Why Celery + Beat

### Celery Worker

Each worker is responsible for executing webhook deliveries:
- Reads delivery tasks from the queue
- Sends HTTP `POST` requests to registered endpoints
- Records delivery results

Workers run concurrently and can be scaled horizontally.

### Celery Beat

Beat acts as a global scheduler, triggering `drain_delivery_queue` every second.

This design ensures:
- **Predictable dispatch rate** — deliveries are metered, not bursty
- **Centralized scheduling logic** — rate limiting lives in one place
- **Consistent rate enforcement** — no worker can exceed the configured limit

---

## Why a Pull-Based Scheduler

Instead of pushing webhook deliveries directly to workers on ingestion, the system uses a pull model:

```
events → Redis queues → scheduler (Beat) → workers
```

**Advantages:**
- Rate limiting is straightforward — the scheduler controls throughput
- Fairness is controllable — round-robin logic lives in one place
- Worker overload is prevented — workers only receive what the scheduler dispatches

If deliveries were pushed directly on ingestion, burst traffic could overwhelm workers and make rate limiting unreliable.

---

## Why Per-User Queues

A single global queue causes **head-of-line blocking**.

**Example with a global FIFO queue:**

| User | Events |
|------|--------|
| A    | 10,000 |
| B    | 1      |

```
Queue: [A A A A A A A A A A A A ... B]
```

User B is forced to wait behind User A's entire backlog — a clear violation of the fairness requirement.

Per-user queues eliminate this by giving each user an independent queue, allowing the scheduler to interleave dispatches across users.

---

## Fairness Strategy

### What Was Chosen: Round-Robin Scheduling

The scheduler iterates across all active users in round-robin order, dispatching one delivery per user per slot until the rate limit is reached.

```python
users = redis.smembers(active_users)

while dispatched < rate:
    pop one delivery from next user
```

**Dispatch order example:**
```
A → B → A → B → A → B → ...
```

### Why This Works

| User | Queue Size |
|------|-----------|
| A    | 1000      |
| B    | 1         |

With `rate = 10/sec`, the scheduler dispatches:
```
A → B → A → A → A → A → A → A → A → A
```

User B's event is processed **immediately** in the first scheduling round — not after User A's 1000-event backlog is drained.

> **Guarantee:** No user can monopolize delivery capacity. Every active user receives a dispatch slot in each scheduling round.

### Alternatives Considered

| Approach | Verdict | Reason |
|---|---|---|
| Global FIFO Queue | ❌ Rejected | Causes head-of-line blocking; User B waits behind User A's full backlog |
| Weighted Fair Queuing | ⚠️ Overkill | More complex Redis state; unnecessary for this workload |
| Separate Celery Queues Per User | ❌ Rejected | Operationally complex; harder to enforce a global rate limit |

### Trade-offs

**Advantages:**
- Simple to implement and reason about
- True fairness across users
- Predictable, observable scheduling behaviour
- Easy to debug — dispatch order is deterministic

**Limitations:**
- Round-robin does not weight users by webhook count (a user with 1 webhook gets the same share as one with 20)
- With an extremely high number of active users, one full round may span more than one second
- Both limitations are acceptable for typical webhook workloads

---

## Why Global Rate Limiting

Without rate limiting, a burst of events can trigger a corresponding burst of outbound HTTP requests:

```
1000 events ingested → 1000 HTTP requests immediately
```

This can overload:
- Webhook receiver endpoints
- Network resources
- Worker pools

The system enforces a single global cap — `deliveries_per_second` — which protects both the system itself and downstream webhook endpoints. The limit is configurable live without restarting workers.

---

## Why Deliveries Are Persisted in the Database

Each webhook delivery creates a `DeliveryAttempt` record in SQLite.

| Benefit | Details |
|---|---|
| **Reliability** | If a worker crashes mid-delivery, the attempt remains recorded and can be inspected |
| **Debugging** | Users can review response status codes, error messages, and timestamps per delivery |
| **Auditability** | Full delivery history is traceable for every event |

---

## Why Retries Are Implemented

Webhook endpoints fail for transient reasons — network timeouts, brief downtime, temporary server errors. The system retries failed deliveries automatically:

- **Max retries:** `3`
- **Backoff strategy:** Exponential — `2^retry` seconds between attempts

This improves end-to-end reliability without overwhelming struggling endpoints.

---

## Why a Mock Receiver

Webhook systems are difficult to test without a controllable receiver. The project includes a built-in mock receiver at `http://localhost:9000` that:
- Accepts incoming `POST` requests
- Logs all received payloads
- Exposes a delivery dashboard at `/logs`

This makes it straightforward to verify rate limiting, fairness behaviour, and end-to-end delivery without any external dependencies.

---

## Observability

The system exposes a monitoring endpoint for real-time inspection:

```http
GET /api/internal/queue-status/
```

```json
{
  "queue": 24,
  "delivered": 150,
  "busy_workers": 6,
  "max_workers": 8,
  "rate": 10
}
```

The web UI includes dashboards for both rate limit testing and multi-user fairness simulation, making system behaviour visible without needing external tooling.

---

## Scalability Considerations

The architecture supports horizontal scaling with minimal changes:

| Component | Scaling Approach |
|---|---|
| **Celery Workers** | Increase `CELERY_CONCURRENCY` or add more worker containers |
| **Redis** | Handles thousands of ops/sec; can manage large queues and many active users |
| **Django API** | Stateless — multiple containers can run behind a load balancer |

---

## Failure Handling

| Failure Mode | Behaviour |
|---|---|
| Worker crash | Tasks remain queued in Redis; Beat reschedules delivery |
| Webhook endpoint failure | Attempt marked failed; retried with exponential backoff |
| API restart | Events and delivery attempts persist in the database; no data loss |

---

## Potential Future Improvements

| Improvement | Benefit |
|---|---|
| Weighted fair queuing | Give users with fewer webhooks proportionally more slots |
| Dead-letter queue | Capture permanently failed deliveries for manual inspection |
| Per-user rate limits | Allow rate caps per customer rather than a single global cap |
| Postgres instead of SQLite | Better concurrency and durability for production workloads |
| HMAC webhook signatures | Allow receivers to verify payload authenticity |
| Metrics export (Prometheus) | Production-grade observability and alerting |

---

## Design Summary

The architecture combines **Redis**, **Celery**, and **Django** to deliver a system that is:

- **Reliable** — persisted delivery attempts with automatic retries
- **Fair** — round-robin scheduling across per-user queues
- **Rate-limited** — global, live-configurable throughput cap
- **Horizontally scalable** — stateless API, concurrent workers, Redis-backed state
- **Easy to debug** — full delivery history, real-time monitoring dashboards

This pattern — per-entity queues with a metered round-robin scheduler — is commonly used in production webhook platforms.