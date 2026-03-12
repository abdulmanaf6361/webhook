# Webhook Delivery System

A reliable, multi-user webhook delivery system built with **Django**, **Celery**, and **Redis**.

## Features

- **Event Ingestion API** — Accepts and stores incoming events
- **Fan-out Webhook Delivery** — Dispatches events to all matching registered webhooks
- **Global Rate Limiting** — Configurable deliveries-per-second, updatable live
- **Fair Multi-User Scheduling** — Round-robin dispatch prevents any single user from monopolizing the pipeline
- **Retry Logic** — Automatic retry handling for failed deliveries
- **Live Monitoring UI** — Real-time dashboards for rate limiting and fairness testing

---

## Quick Start

```bash
git clone https://github.com/abdulmanaf6361/webhook.git
cd webhook
docker compose up
```

Once containers are running, open the following services:

| Service | URL | Purpose |
|---|---|---|
| Web UI + API | http://127.0.0.1:8000 | Webhook management and testing UI |
| Mock Receiver | http://127.0.0.1:9000 | Simulated customer webhook endpoint |
| Receiver Logs | http://127.0.0.1:9000/logs | Raw JSON logs of received webhooks |

> No manual setup required.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│    Docker Compose(5 Services and 1 External Cloud DB)    │
│                                                          │
│   ┌─────────────┐                                        │
│   │   Django    │  API + UI                              │
│   │   Web App   |  http://localhost:8000                 |
|   |Both FrontEnd|                                        |
|   |    and      |                                        |
|   |  Backend    │                                        │
│   └──────┬──────┘                                        │
│          │                                               │
│          ▼                                               │
│   ┌─────────────┐      ┌─────────────┐                   │
│   │   Redis     │◄────►│   Celery    │                   │
│   │             │      │   Workers   │                   │ 
│   │ Per-user    │      │             │                   │
│   │ queues      │      │Send webhooks│                   │
│   └──────┬──────┘      └──────┬──────┘                   │
│          │                    │                          │
│          ▼                    ▼                          │
│     Celery Beat          Mock Receiver                   │
│     (1 second tick)      http://localhost:9000           │
│                                                          │
│               ┌────────────────────────┐                 │
│               │       Postgres DB      │                 │
│               │ webhooks, events,      │                 │
│               │ delivery attempts      │                 │
│               └────────────────────────┘                 │
└──────────────────────────────────────────────────────────┘
```

### Event Delivery Flow

```
Client
  │
  │  POST /api/events/ingest/
  ▼
Django API
  │  create Event
  │  create DeliveryAttempt(s)
  ▼
Redis
  │  push delivery id to
  │  webhook:user:{user_id}:queue
  ▼
Celery Beat (every 1 second)
  │  runs drain_delivery_queue
  ▼
Celery Worker
  │  sends HTTP POST to webhook endpoint
  ▼
Mock Receiver
```

### Redis Data Structures

**Per-user delivery queues:**
```
webhook:user:<user_id>:queue

# Examples:
webhook:user:user_A:queue
webhook:user:user_B:queue
```

**Active users set:**
```
webhook:active_users
```
Used by the scheduler to perform fair round-robin dispatch.

**Rate limit key:**
```
webhook:rate_limit
```

---

## API Reference

### Authentication

All webhook management endpoints require a simulated user header. No real authentication layer is implemented.

```
X-User-Id: test_user
```

---

### Webhook Management

#### Register a Webhook
```http
POST /api/webhooks/
X-User-Id: test_user
Content-Type: application/json

{
  "url": "http://mock_receiver:9000/",
  "event_types": ["request.created", "request.updated"]
}
```

**Response:**
```json
{
  "id": "uuid",
  "user_id": "test_user",
  "url": "http://mock_receiver:9000/",
  "event_types": ["request.created", "request.updated"],
  "is_active": true
}
```

#### List Webhooks
```http
GET /api/webhooks/
GET /api/webhooks/?status=active
GET /api/webhooks/?status=disabled
```

#### Update Webhook
```http
PUT   /api/webhooks/<id>/
PATCH /api/webhooks/<id>/
```

#### Delete Webhook
```http
DELETE /api/webhooks/<id>/
```

#### Toggle Webhook (enable/disable)
```http
POST /api/webhooks/<id>/toggle/
```

#### Delivery History
```http
GET /api/webhooks/<id>/deliveries/
```
Returns recent delivery attempts for the specified webhook.

---

### Event Ingestion

```http
POST /api/events/ingest/
Content-Type: application/json

{
  "user_id": "test_user",
  "event_type": "request.created",
  "payload": {
    "request_id": "123"
  }
}
```

**Response:**
```json
{
  "event_id": "...",
  "deliveries_queued": 2
}
```

**Valid event types:**
- `request.created`
- `request.updated`
- `request.deleted`

---

### Rate Limit Configuration

#### View current rate limit
```http
GET /api/internal/rate-limit/
```

#### Update rate limit (takes effect immediately)
```http
PUT /api/internal/rate-limit/
Content-Type: application/json

{
  "deliveries_per_second": 10
}
```

---

### Queue Monitoring

```http
GET /api/internal/queue-status/
```

**Response:**
```json
{
  "queue": 24,
  "delivered": 150,
  "busy_workers": 6,
  "max_workers": 8,
  "rate": 10
}
```

---

## Acceptance Criteria

### Part A — Core Delivery

1. Register a webhook
2. Publish an event → verify webhook receives `POST`
3. Disable webhook → publish event → **no delivery**
4. Enable webhook → publish event → **delivery resumes**

The mock receiver UI at `http://localhost:9000/logs` shows all received events.

### Part B — Rate Limiting

Set a rate limit and publish a burst of events:

```http
PUT /api/internal/rate-limit/
{ "deliveries_per_second": 5 }
```

Publish 20 events — deliveries should spread over ~4 seconds.

The rate limit can also be updated **live** (e.g. `5/sec → 20/sec`) without restarting workers.

### Part C — Multi-User Fairness

**Goal:** One user flooding the system must not block other users.

**Test scenario:**
- Rate limit = `10/sec`
- User A publishes `1000` events
- User B publishes `1` event

**Expected behaviour:** User B's delivery arrives within a few seconds — not after User A's full backlog is drained.

---

## Fairness Strategy

### Per-User Queues

Each user has an independent Redis queue:

```
webhook:user:{user_id}:queue
```

### Round-Robin Scheduler

Celery Beat runs `drain_delivery_queue` every second. The scheduler iterates across all active users in round-robin order:

```python
users = redis.smembers(active_users)

while dispatched < rate:
    pop one delivery from next user
```

**Dispatch order example:**
```
A → B → A → B → A → B → ...
```

This guarantees no single user can monopolize the delivery pipeline.

**Example with an imbalanced load:**

| User | Queue Size |
|------|-----------|
| A    | 1000      |
| B    | 1         |

With `rate = 10/sec`, User B's event is interleaved immediately in the next scheduling round — not queued behind User A's 1000 events.

### Alternatives Considered

| Approach | Verdict | Reason |
|---|---|---|
| Global FIFO Queue | ❌ Rejected | User B waits behind User A's full backlog |
| Weighted Fair Queuing | ⚠️ Overkill | Adds Redis complexity without meaningful benefit for this workload |
| Separate Celery Queues Per User | ❌ Rejected | Operationally complex and harder to scale |

### Trade-offs

**Advantages:**
- Simple architecture
- True fairness across users
- Predictable, observable scheduling
- Easy to debug

**Limitations:**
- Round-robin does not weight users by webhook count
- With an extremely high number of active users, one round may span multiple seconds
- Acceptable for typical webhook workloads

---

## Project Structure

```
webhook_system/
│
├── docker-compose.yml
├── Dockerfile
├── entrypoint.sh
├── manage.py
│
├── webhook/
│   ├── settings.py
│   ├── celery.py
│   └── urls.py
│
├── webhook_app/
│   ├── models.py
│   ├── views.py
│   ├── tasks.py
│   ├── urls.py
│   └── migrations/
│
├── templates/
│   └── webhook_app/
|       ├── base.html
│       ├── index.html
│       ├── deliveries.html
|       ├── event.html
│       ├── webhook_detail.html
│       ├── webhook_form.html
│       ├── test_ratelimit.html
│       └── fairness_test.html
│
└── mock_receiver/
    ├── Dockerfile
    └── receiver.py
```

---

## Built-in Testing UIs

### Rate Limit Monitor

Accessible from the web UI. Displays:
- Current queue size
- Total delivered count
- Active vs. max worker usage
- Live rate behaviour graph

Used to verify **Part B**.

### Multi-User Fairness Test

Simulates a flood scenario and displays:
- Per-user queue sizes in real time
- Delivery timestamps for each user
- Visual proof of fair scheduling

Used to verify **Part C**.

---

## Summary

| Capability | Details |
|---|---|
| Delivery | Reliable HTTP POST to registered endpoints |
| Storage | Postgresql — events, webhooks, delivery attempts |
| Queueing | Redis per-user queues |
| Rate Limiting | Global, live-configurable deliveries/sec |
| Fairness | Round-robin multi-user scheduler |
| Retries | Automatic retry on delivery failure |
| Monitoring | Real-time queue and fairness dashboards |