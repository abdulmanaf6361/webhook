"""
tasks.py — Celery task definitions
====================================

Flow overview:
  1. POST /api/events/ingest/
       → validates request
       → looks up matching active webhooks (DB read, fast)
       → pushes one raw JSON message per (event x webhook) onto INGEST_QUEUE_KEY
       → returns event_id immediately  ← no DB write in request path

  2. flush_ingest_queue  (Beat, every 2s)
       → pops up to FLUSH_BATCH_SIZE items from INGEST_QUEUE_KEY
       → bulk_create Event rows
       → bulk_create DeliveryAttempt rows (status=pending)
       → pushes delivery IDs into per-user fair queues

  3. process_delivery_queue  (Beat, every 1s)
       → consumes token-bucket budget
       → round-robin dequeues from per-user queues
       → dispatches execute_delivery.delay() per item

  4. execute_delivery  (gevent worker, 1000 concurrent)
       → HTTP POST to webhook URL
       → pushes {id, status, code, error, delivered_at} onto WRITEBACK_QUEUE_KEY

  5. flush_writeback_queue  (Beat, every 2s)
       → pops from WRITEBACK_QUEUE_KEY
       → bulk_update DeliveryAttempt rows in one query
"""

import time
import json
import uuid
import logging
import requests

from datetime import timezone as dt_timezone
from datetime import datetime

from celery import shared_task
import redis as redis_lib

from django.conf import settings
from django.utils import timezone as django_tz

logger = logging.getLogger(__name__)

# ── Batch sizes ────────────────────────────────────────────────────────────────
FLUSH_BATCH_SIZE    = int(getattr(settings, 'FLUSH_BATCH_SIZE',    500))
WRITEBACK_BATCH_SIZE = int(getattr(settings, 'WRITEBACK_BATCH_SIZE', 500))


# ── Redis client ───────────────────────────────────────────────────────────────
def get_redis():
    return redis_lib.from_url(settings.REDIS_URL, decode_responses=True)


# ── Rate limit helpers ─────────────────────────────────────────────────────────

def get_rate_limit():
    r = get_redis()
    val = r.get(settings.RATE_LIMIT_REDIS_KEY)
    if val:
        return int(val)
    try:
        from webhook_app.models import RateLimitConfig
        config = RateLimitConfig.get_instance()
        r.set(settings.RATE_LIMIT_REDIS_KEY, config.deliveries_per_second)
        return config.deliveries_per_second
    except Exception:
        return settings.DEFAULT_RATE_LIMIT


def set_rate_limit(deliveries_per_second: int):
    r = get_redis()
    r.set(settings.RATE_LIMIT_REDIS_KEY, deliveries_per_second)
    r.delete(settings.RATE_LIMIT_BUCKET_KEY)
    from webhook_app.models import RateLimitConfig
    config = RateLimitConfig.get_instance()
    config.deliveries_per_second = deliveries_per_second
    config.save()

# ---------------------------------------------------------------------------
# Global Token Bucket Rate Limiter (Redis + Lua)
#
# Implements an atomic, system-wide rate limiter using the Token Bucket
# algorithm stored in Redis. The Lua script executes inside Redis, ensuring
# the entire check-and-update operation is atomic and race-safe even when
# many Celery workers run concurrently.
#
# Concept
# -------
# A "bucket" holds tokens representing how many deliveries are allowed.
#
#   capacity = rate (deliveries per second)
#
# Tokens refill over time:
#
#   tokens += elapsed_time * rate
#
# but never exceed the bucket capacity. Each delivery consumes one token.
#
# If a token is available → delivery allowed.
# If no tokens remain → delivery must wait.
#
# Example
# -------
# rate = 5/sec
#
# time=0s   tokens=5
# deliver   → tokens=4
# deliver   → tokens=3
# ...
# deliver   → tokens=0
#
# next call → denied until tokens refill.
#
# Why Lua?
# --------
# Without Lua, multiple workers could read the same token count and exceed
# the rate limit. Lua runs atomically inside Redis, guaranteeing correctness.
#
# Stored Redis value
# ------------------
# Key: RATE_LIMIT_BUCKET_KEY
#
# {
#     "tokens": <int>,
#     "last_time": <timestamp>
# }
#
# The key has a TTL so idle systems automatically clean up bucket state.
# ---------------------------------------------------------------------------

_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local now = tonumber(ARGV[2])
local want = tonumber(ARGV[3])

local raw = redis.call('GET', key)

local tokens
local last_time

if raw == false then
    tokens = 0
    last_time = now
else
    local data = cjson.decode(raw)
    tokens = data['tokens']
    last_time = data['last_time']
end

local elapsed = now - last_time
tokens = math.min(rate, math.floor(tokens + elapsed * rate))

local allowed = math.min(tokens, want)

tokens = tokens - allowed

redis.call(
    'SET',
    key,
    cjson.encode({tokens=tokens, last_time=now}),
    'EX',
    60
)

return allowed
"""


def acquire_tokens(max_tokens: int) -> int:
    r = get_redis()
    rate = get_rate_limit()
    now = time.time()

    return r.eval(
        _TOKEN_BUCKET_LUA,
        1,
        settings.RATE_LIMIT_BUCKET_KEY,
        rate,
        now,
        max_tokens,
    )

# ── Fair queue helpers ─────────────────────────────────────────────────────────

def enqueue_delivery(user_id: str, delivery_id: str):
    r = get_redis()

    queue_key = f"{settings.USER_QUEUE_KEY_PREFIX}{user_id}"

    pipe = r.pipeline()

    # push delivery to user's queue
    pipe.rpush(queue_key, delivery_id)

    # ensure user is present in fairness queue only once
    pipe.lrem(settings.FAIRNESS_QUEUE_KEY, 0, user_id)
    pipe.rpush(settings.FAIRNESS_QUEUE_KEY, user_id)

    pipe.execute()

def dequeue_delivery_fair():
    r = get_redis()

    # rotate fairness queue
    user_id = r.lpop(settings.FAIRNESS_QUEUE_KEY)
    if not user_id:
        return None, None

    r.rpush(settings.FAIRNESS_QUEUE_KEY, user_id)

    queue_key = f"{settings.USER_QUEUE_KEY_PREFIX}{user_id}"
    delivery_id = r.lpop(queue_key)

    if delivery_id:
        return user_id, delivery_id

    return None, None
# ── Async ingest helpers ───────────────────────────────────────────────────────

def push_ingest_message(event_id: str, user_id: str, event_type: str,
                        payload: dict, webhook_id: str, delivery_id: str):
    """
    Push one (event, webhook) pair onto the ingest queue.
    Called from the ingest view — no DB writes at all.
    Multiple pairs for the same event share the same event_id but have
    different delivery_ids. flush_ingest_queue deduplicates Event rows.
    """
    r   = get_redis()
    msg = json.dumps({
        'event_id':    event_id,
        'user_id':     user_id,
        'event_type':  event_type,
        'payload':     payload,
        'webhook_id':  webhook_id,
        'delivery_id': delivery_id,
    })
    r.rpush(settings.INGEST_QUEUE_KEY, msg)


def push_writeback(delivery_id: str, status: str, response_status_code,
                   response_body: str, error_message: str, delivered_at: str):
    """Push delivery result onto the write-back queue."""
    r   = get_redis()
    msg = json.dumps({
        'delivery_id':          delivery_id,
        'status':               status,
        'response_status_code': response_status_code,
        'response_body':        response_body,
        'error_message':        error_message,
        'delivered_at':         delivered_at,
    })
    r.rpush(settings.WRITEBACK_QUEUE_KEY, msg)


# ── Task 1: flush_ingest_queue ─────────────────────────────────────────────────

@shared_task(name='webhook_app.tasks.flush_ingest_queue')
def flush_ingest_queue():
    """
    Beat-scheduled every 2s.

    Pops up to FLUSH_BATCH_SIZE messages from INGEST_QUEUE_KEY.
    - bulk_create Event rows  (one per unique event_id)
    - bulk_create DeliveryAttempt rows
    - enqueue delivery IDs into per-user fair queues
    """
    from webhook_app.models import Event, DeliveryAttempt

    r        = get_redis()
    pipe     = r.pipeline()
    for _ in range(FLUSH_BATCH_SIZE):
        pipe.lpop(settings.INGEST_QUEUE_KEY)
    raw_items = pipe.execute()
    items     = [json.loads(x) for x in raw_items if x]

    if not items:
        return

    # Deduplicate events — multiple deliveries share the same event_id
    events_by_id = {}
    for item in items:
        eid = item['event_id']
        if eid not in events_by_id:
            events_by_id[eid] = Event(
                id         = eid,
                user_id    = item['user_id'],
                event_type = item['event_type'],
                payload    = item['payload'],
            )

    # bulk_create Events — ignore conflicts (idempotent if flushed twice)
    Event.objects.bulk_create(
        list(events_by_id.values()),
        ignore_conflicts=True,
    )

    # bulk_create DeliveryAttempts
    attempts = []
    for item in items:
        attempts.append(DeliveryAttempt(
            id         = item['delivery_id'],
            webhook_id = item['webhook_id'],
            event_id   = item['event_id'],
            status     = 'pending',
        ))
    DeliveryAttempt.objects.bulk_create(attempts, ignore_conflicts=True)

    # Enqueue into fair per-user queues
    # We need user_id per delivery — grab from the item directly
    for item in items:
        enqueue_delivery(item['user_id'], item['delivery_id'])

    logger.info(f"flush_ingest_queue: persisted {len(events_by_id)} events, "
                f"{len(attempts)} deliveries")

# ── Task 2: process_delivery_queue ────────────────────────────────────────────

@shared_task(name='webhook_app.tasks.process_delivery_queue')
def process_delivery_queue():
    """
    Dispatcher task scheduled by Celery Beat.

    Responsibilities
    ----------------
    1. Acquire delivery budget from the global token bucket rate limiter.
    2. Dequeue deliveries fairly across users.
    3. Dispatch execute_delivery Celery tasks.

    Properties
    ----------
    - Never performs HTTP requests itself.
    - Never touches the database.
    - Runs in <10ms regardless of queue size.
    - Fully controlled by the global rate limit.

    Delivery rate equation:
        total_time ≈ events / rate
    """

    rate = get_rate_limit()

    # Ask Redis token bucket how many deliveries are allowed this tick
    budget = acquire_tokens(rate)

    if budget <= 0:
        return

    dispatched = 0

    while dispatched < budget:
        user_id, delivery_id = dequeue_delivery_fair()

        # No more deliveries available
        if not delivery_id:
            break

        execute_delivery.delay(delivery_id)
        dispatched += 1

    if dispatched:
        logger.info(
            f"[rate={rate}/s] dispatched {dispatched}/{budget} deliveries this tick"
        )

# ── Task 3: execute_delivery ──────────────────────────────────────────────────

@shared_task(name='webhook_app.tasks.execute_delivery')
def execute_delivery(delivery_attempt_id: str):
    """
    Runs in gevent worker — up to 1000 concurrent greenlets.

    Reads the DeliveryAttempt + related Webhook/Event from DB,
    performs the HTTP POST, then pushes the result onto WRITEBACK_QUEUE_KEY
    instead of writing to DB directly.

    DB reads here are fast (indexed PK lookups, pool connection reuse).
    DB write is deferred to flush_writeback_queue.
    """
    from webhook_app.models import DeliveryAttempt

    try:
        attempt = DeliveryAttempt.objects.select_related('webhook', 'event').get(
            id=delivery_attempt_id
        )
    except DeliveryAttempt.DoesNotExist:
        logger.error(f"DeliveryAttempt {delivery_attempt_id} not found")
        return

    if not attempt.webhook.is_active:
        push_writeback(
            delivery_id=delivery_attempt_id,
            status='failed',
            response_status_code=None,
            response_body='',
            error_message='Webhook was disabled before delivery',
            delivered_at=datetime.now(dt_timezone.utc).isoformat(),
        )
        logger.info(f"Skipped {delivery_attempt_id}: webhook disabled")
        return

    payload = {
        'event_id':   str(attempt.event.id),
        'event_type': attempt.event.event_type,
        'user_id':    attempt.event.user_id,
        'payload':    attempt.event.payload,
        'webhook_id': str(attempt.webhook.id),
        'timestamp':  attempt.event.created_at.isoformat(),
    }

    status               = 'failed'
    response_status_code = None
    response_body        = ''
    error_message        = ''

    try:
        response = requests.post(
            attempt.webhook.url,
            json=payload,
            timeout=10,
            headers={
                'Content-Type':    'application/json',
                'X-Webhook-Event': attempt.event.event_type,
            },
        )
        response_status_code = response.status_code
        response_body        = response.text[:1000]

        if 200 <= response.status_code < 300:
            status = 'success'
            logger.info(f"✓ {delivery_attempt_id} → {response.status_code}")
        else:
            error_message = f"Non-2xx response: {response.status_code}"
            logger.warning(f"✗ {delivery_attempt_id} → {response.status_code}")

    except requests.exceptions.Timeout:
        error_message = 'Request timed out'
        logger.warning(f"✗ Timeout {delivery_attempt_id}")
    except requests.exceptions.ConnectionError as exc:
        error_message = f"Connection error: {exc}"
        logger.warning(f"✗ ConnError {delivery_attempt_id}")
    except Exception as exc:
        error_message = f"Unexpected error: {exc}"
        logger.error(f"✗ Unexpected {delivery_attempt_id}: {exc}")

    push_writeback(
        delivery_id=delivery_attempt_id,
        status=status,
        response_status_code=response_status_code,
        response_body=response_body,
        error_message=error_message,
        delivered_at=datetime.now(dt_timezone.utc).isoformat(),
    )


# ── Task 4: flush_writeback_queue ─────────────────────────────────────────────

@shared_task(name='webhook_app.tasks.flush_writeback_queue')
def flush_writeback_queue():
    """
    Beat-scheduled every 2s.

    Pops from WRITEBACK_QUEUE_KEY and bulk-updates DeliveryAttempt rows.
    One UPDATE query for up to WRITEBACK_BATCH_SIZE rows — far cheaper
    than individual saves per delivery.
    """
    from webhook_app.models import DeliveryAttempt

    r        = get_redis()
    pipe     = r.pipeline()
    for _ in range(WRITEBACK_BATCH_SIZE):
        pipe.lpop(settings.WRITEBACK_QUEUE_KEY)
    raw_items = pipe.execute()
    items     = [json.loads(x) for x in raw_items if x]

    if not items:
        return

    # Build lookup by id
    updates = {item['delivery_id']: item for item in items}
    ids     = list(updates.keys())

    # Fetch existing rows in one query
    attempts = {str(a.id): a for a in DeliveryAttempt.objects.filter(id__in=ids)}

    for did, item in updates.items():
        attempt = attempts.get(did)
        if not attempt:
            continue
        attempt.status               = item['status']
        attempt.response_status_code = item['response_status_code']
        attempt.response_body        = item.get('response_body', '')
        attempt.error_message        = item.get('error_message', '')
        if item.get('delivered_at'):
            try:
                attempt.delivered_at = datetime.fromisoformat(item['delivered_at'])
            except (ValueError, TypeError):
                attempt.delivered_at = django_tz.now()

    DeliveryAttempt.objects.bulk_update(
        list(attempts.values()),
        ['status', 'response_status_code', 'response_body',
         'error_message', 'delivered_at'],
    )

    success = sum(1 for i in items if i['status'] == 'success')
    failed  = len(items) - success
    logger.info(f"flush_writeback_queue: updated {len(items)} attempts "
                f"({success} ok, {failed} failed)")


# ── Legacy task: deliver_webhook (kept for backwards compatibility) ─────────
# Previously called from ingest view. Now ingest uses push_ingest_message directly.
# Keeping this so any tasks already queued in Redis still execute cleanly.

@shared_task(bind=True, name='webhook_app.tasks.deliver_webhook')
def deliver_webhook(self, delivery_attempt_id: str):
    """Legacy fanout task — enqueues directly without DB flush."""
    from webhook_app.models import DeliveryAttempt
    try:
        attempt = DeliveryAttempt.objects.select_related('event').get(id=delivery_attempt_id)
    except DeliveryAttempt.DoesNotExist:
        logger.error(f"deliver_webhook: {delivery_attempt_id} not found")
        return
    enqueue_delivery(attempt.event.user_id, delivery_attempt_id)