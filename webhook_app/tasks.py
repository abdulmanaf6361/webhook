import time
import logging
import requests

from celery import shared_task
import redis as redis_lib

from django.conf import settings
from django.utils import timezone as django_tz

logger = logging.getLogger(__name__)

# ── Redis client ───────────────────────────────────────────────────────────────
def get_redis():
    return redis_lib.from_url(settings.REDIS_URL, decode_responses=True)


# ── Rate limit helpers ─────────────────────────────────────────────────────────

def get_rate_limit():
    """Get current rate limit from Redis, fallback to DB, fallback to default."""
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
    """Persist rate limit to Redis (instant) and DB (durable)."""
    r = get_redis()
    r.set(settings.RATE_LIMIT_REDIS_KEY, deliveries_per_second)
    # Reset the token bucket so the new rate takes effect immediately
    r.delete(settings.RATE_LIMIT_BUCKET_KEY)
    from webhook_app.models import RateLimitConfig
    config = RateLimitConfig.get_instance()
    config.deliveries_per_second = deliveries_per_second
    config.save()


# Lua script for atomic token-bucket check-and-consume.
# Returns 1 if a token was consumed, 0 if the bucket is empty.
# Args: KEYS[1]=bucket_key, ARGV[1]=rate, ARGV[2]=now (float seconds)
_TOKEN_BUCKET_LUA = """
local key      = KEYS[1]
local rate     = tonumber(ARGV[1])
local now      = tonumber(ARGV[2])
local raw      = redis.call('GET', key)
local tokens, last_time

if raw == false then
    -- First call: start with 0 tokens (not full) to avoid burst at startup
    tokens    = 0
    last_time = now
else
    local data = cjson.decode(raw)
    tokens    = data['tokens']
    last_time = data['last_time']
end

-- Refill proportional to elapsed time, capped at rate
local elapsed = now - last_time
tokens = math.min(rate, tokens + elapsed * rate)

if tokens >= 1 then
    tokens = tokens - 1
    redis.call('SET', key, cjson.encode({tokens=tokens, last_time=now}), 'EX', 60)
    return 1
else
    -- Store the refilled (but still < 1) token count so refill is continuous
    redis.call('SET', key, cjson.encode({tokens=tokens, last_time=now}), 'EX', 60)
    return 0
end
"""

def try_acquire_token() -> bool:
    """
    Atomically attempt to consume one token from the bucket.
    Returns True if successful (caller may proceed), False if bucket empty.
    This is atomic via a Lua script — safe under concurrent workers.
    """
    r = get_redis()
    rate = get_rate_limit()
    now = time.time()
    result = r.eval(_TOKEN_BUCKET_LUA, 1, settings.RATE_LIMIT_BUCKET_KEY, rate, now)
    return result == 1


# ── Fair queue helpers (Part C) ────────────────────────────────────────────────
# Per-user FIFO Redis lists + a Redis set of currently active users.
# process_delivery_queue round-robins across active users, one slot per user per round.

USER_QUEUE_PREFIX = 'webhook:user_queue:'
ACTIVE_USERS_KEY  = 'webhook:active_users'


def enqueue_delivery(user_id: str, delivery_id: str):
    """Push a delivery into the per-user queue and mark the user as active."""
    r = get_redis()
    queue_key = f"{USER_QUEUE_PREFIX}{user_id}"
    pipe = r.pipeline()
    pipe.rpush(queue_key, delivery_id)
    pipe.sadd(ACTIVE_USERS_KEY, user_id)
    pipe.execute()


def dequeue_delivery_fair():
    """
    Round-robin pop across all active users.
    Returns (user_id, delivery_id) or (None, None) when all queues are empty.
    """
    r = get_redis()
    users = r.smembers(ACTIVE_USERS_KEY)
    if not users:
        return None, None

    for user_id in list(users):
        queue_key = f"{USER_QUEUE_PREFIX}{user_id}"
        delivery_id = r.lpop(queue_key)
        if delivery_id:
            if r.llen(queue_key) == 0:
                r.srem(ACTIVE_USERS_KEY, user_id)
            return user_id, delivery_id

    # All queues found empty — clean up stale active set
    r.delete(ACTIVE_USERS_KEY)
    return None, None


# ── Celery tasks ───────────────────────────────────────────────────────────────

@shared_task(bind=True, name='webhook_app.tasks.deliver_webhook')
def deliver_webhook(self, delivery_attempt_id: str):
    """
    Fanout task: called once per (event × webhook) pair.
    Pushes the delivery ID into the per-user Redis queue.
    Actual HTTP delivery happens in process_delivery_queue.
    """
    from webhook_app.models import DeliveryAttempt
    try:
        attempt = DeliveryAttempt.objects.select_related('event').get(id=delivery_attempt_id)
    except DeliveryAttempt.DoesNotExist:
        logger.error(f"DeliveryAttempt {delivery_attempt_id} not found")
        return

    user_id = attempt.event.user_id
    enqueue_delivery(user_id, delivery_attempt_id)
    logger.info(f"Enqueued delivery {delivery_attempt_id} for user {user_id}")


@shared_task(name='webhook_app.tasks.process_delivery_queue')
def process_delivery_queue():
    """
    Beat-scheduled every second.

    Design:
    - Ask the token bucket how many tokens are available this tick (= rate/s).
    - Dequeue up to that many deliveries (fair, round-robin across users).
    - Execute each delivery synchronously inside this task.

    Why not block inside the task waiting for tokens?
    Blocking would hold the Celery worker thread and cause Beat to queue up
    overlapping instances of this task, eventually exhausting the worker pool.
    Instead we dequeue only as many items as the bucket currently allows,
    and leave the rest for the next tick — which is what "rate limiting" means.
    """
    rate     = get_rate_limit()
    budget   = 0  # how many deliveries we're allowed this tick

    # Drain the token bucket: consume every available token right now.
    # Because the bucket refills at `rate` tokens/second and Beat fires every
    # second, this will typically yield exactly `rate` tokens (or fewer if the
    # queue was idle and the bucket hadn't fully refilled yet).
    for _ in range(rate):
        if try_acquire_token():
            budget += 1
        else:
            break   # bucket empty — no more tokens available this tick

    if budget == 0:
        return  # nothing to do

    processed = 0
    while processed < budget:
        user_id, delivery_id = dequeue_delivery_fair()
        if delivery_id is None:
            break  # queue drained — less work than budget, that's fine
        _execute_delivery(delivery_id)
        processed += 1

    if processed:
        logger.info(f"[rate={rate}/s] Processed {processed}/{budget} deliveries this tick")


def _execute_delivery(delivery_attempt_id: str):
    """Perform the actual HTTP POST and record the result."""
    from webhook_app.models import DeliveryAttempt
    try:
        attempt = DeliveryAttempt.objects.select_related('webhook', 'event').get(
            id=delivery_attempt_id
        )
    except DeliveryAttempt.DoesNotExist:
        logger.error(f"DeliveryAttempt {delivery_attempt_id} not found during execution")
        return

    if not attempt.webhook.is_active:
        attempt.status = 'failed'
        attempt.error_message = 'Webhook was disabled before delivery'
        attempt.delivered_at = django_tz.now()
        attempt.save()
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
        attempt.response_status_code = response.status_code
        attempt.response_body        = response.text[:1000]

        if 200 <= response.status_code < 300:
            attempt.status = 'success'
            logger.info(f"✓ Delivered {delivery_attempt_id} → {response.status_code}")
        else:
            attempt.status        = 'failed'
            attempt.error_message = f"Non-2xx response: {response.status_code}"
            logger.warning(f"✗ Failed {delivery_attempt_id} → {response.status_code}")

    except requests.exceptions.Timeout:
        attempt.status        = 'failed'
        attempt.error_message = 'Request timed out'
        logger.warning(f"✗ Timeout {delivery_attempt_id}")
    except requests.exceptions.ConnectionError as exc:
        attempt.status        = 'failed'
        attempt.error_message = f"Connection error: {exc}"
        logger.warning(f"✗ Connection error {delivery_attempt_id}: {exc}")
    except Exception as exc:
        attempt.status        = 'failed'
        attempt.error_message = f"Unexpected error: {exc}"
        logger.error(f"✗ Unexpected error {delivery_attempt_id}: {exc}")

    attempt.delivered_at = django_tz.now()
    attempt.save()
