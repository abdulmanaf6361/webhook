import time
import json
import logging
import requests
from datetime import datetime, timezone

from celery import shared_task
import redis as redis_lib

from django.conf import settings
from django.utils import timezone as django_tz

logger = logging.getLogger(__name__)

# ── Redis client (shared across helpers) ──────────────────────────────────────
def get_redis():
    return redis_lib.from_url(settings.REDIS_URL, decode_responses=True)


# ── Rate limit helpers ─────────────────────────────────────────────────────────

def get_rate_limit():
    """Get current rate limit from Redis (fallback to DB then default)."""
    r = get_redis()
    val = r.get(settings.RATE_LIMIT_REDIS_KEY)
    if val:
        return int(val)
    # Fallback: read from DB and cache in Redis
    try:
        from webhook_app.models import RateLimitConfig
        config = RateLimitConfig.get_instance()
        r.set(settings.RATE_LIMIT_REDIS_KEY, config.deliveries_per_second)
        return config.deliveries_per_second
    except Exception:
        return settings.DEFAULT_RATE_LIMIT


def set_rate_limit(deliveries_per_second: int):
    """Persist rate limit to both Redis and DB."""
    r = get_redis()
    r.set(settings.RATE_LIMIT_REDIS_KEY, deliveries_per_second)
    from webhook_app.models import RateLimitConfig
    config = RateLimitConfig.get_instance()
    config.deliveries_per_second = deliveries_per_second
    config.save()


def acquire_rate_limit_slot():
    """
    Token-bucket rate limiter implemented in Redis.
    Blocks (with small sleeps) until a delivery slot is available.
    Returns when the caller may proceed with one delivery.
    """
    r = get_redis()
    while True:
        rate = get_rate_limit()
        now = time.time()
        bucket_key = settings.RATE_LIMIT_BUCKET_KEY

        pipe = r.pipeline()
        pipe.get(bucket_key)
        results = pipe.execute()
        raw = results[0]

        if raw is None:
            # Initialise bucket: full tokens, current timestamp
            tokens = rate
            last_time = now
        else:
            data = json.loads(raw)
            tokens = data['tokens']
            last_time = data['last_time']

        # Refill tokens based on elapsed time
        elapsed = now - last_time
        tokens = min(rate, tokens + elapsed * rate)
        last_time = now

        if tokens >= 1:
            tokens -= 1
            r.set(bucket_key, json.dumps({'tokens': tokens, 'last_time': last_time}))
            return  # slot acquired
        else:
            # Wait a short time and retry
            time.sleep(0.05)


# ── Fair queue helpers (Part C) ────────────────────────────────────────────────
# Strategy: Per-user FIFO queues + a round-robin active-user set.
# When a delivery is enqueued: push to user's list + add user to active set.
# Worker pops one user at a time (round-robin) and processes one delivery per turn.

USER_QUEUE_PREFIX = 'webhook:user_queue:'
ACTIVE_USERS_KEY = 'webhook:active_users'


def enqueue_delivery(user_id: str, delivery_id: str):
    """Push a delivery into the per-user queue and mark user active."""
    r = get_redis()
    queue_key = f"{USER_QUEUE_PREFIX}{user_id}"
    pipe = r.pipeline()
    pipe.rpush(queue_key, delivery_id)
    pipe.sadd(ACTIVE_USERS_KEY, user_id)
    pipe.execute()


def dequeue_delivery_fair():
    """
    Round-robin across active users.
    Returns (user_id, delivery_id) or (None, None) if all queues are empty.
    """
    r = get_redis()
    users = r.smembers(ACTIVE_USERS_KEY)
    if not users:
        return None, None

    for user_id in users:
        queue_key = f"{USER_QUEUE_PREFIX}{user_id}"
        delivery_id = r.lpop(queue_key)
        if delivery_id:
            # If user queue now empty, remove from active set
            if r.llen(queue_key) == 0:
                r.srem(ACTIVE_USERS_KEY, user_id)
            return user_id, delivery_id

    # All queues were empty
    r.delete(ACTIVE_USERS_KEY)
    return None, None


# ── Core delivery task ─────────────────────────────────────────────────────────

@shared_task(bind=True, name='webhook_app.tasks.deliver_webhook')
def deliver_webhook(self, delivery_attempt_id: str):
    """
    Called by the fan-out task. Enqueues delivery into fair per-user queue.
    The actual HTTP POST happens via process_delivery_queue.
    """
    from webhook_app.models import DeliveryAttempt
    try:
        attempt = DeliveryAttempt.objects.select_related('webhook', 'event').get(id=delivery_attempt_id)
    except DeliveryAttempt.DoesNotExist:
        logger.error(f"DeliveryAttempt {delivery_attempt_id} not found")
        return

    user_id = attempt.event.user_id
    enqueue_delivery(user_id, delivery_attempt_id)
    logger.info(f"Enqueued delivery {delivery_attempt_id} for user {user_id}")


@shared_task(name='webhook_app.tasks.process_delivery_queue')
def process_delivery_queue():
    """
    Periodic task that drains the fair queue respecting the rate limit.
    Scheduled every second via Celery beat.
    """
    from webhook_app.models import DeliveryAttempt
    rate = get_rate_limit()
    processed = 0

    while processed < rate:
        user_id, delivery_id = dequeue_delivery_fair()
        if delivery_id is None:
            break

        acquire_rate_limit_slot()
        _execute_delivery(delivery_id)
        processed += 1

    if processed:
        logger.info(f"Processed {processed} deliveries this tick")


def _execute_delivery(delivery_attempt_id: str):
    """Perform the actual HTTP POST for a delivery attempt."""
    from webhook_app.models import DeliveryAttempt
    try:
        attempt = DeliveryAttempt.objects.select_related('webhook', 'event').get(id=delivery_attempt_id)
    except DeliveryAttempt.DoesNotExist:
        logger.error(f"DeliveryAttempt {delivery_attempt_id} not found during execution")
        return

    if not attempt.webhook.is_active:
        attempt.status = 'failed'
        attempt.error_message = 'Webhook was disabled before delivery'
        attempt.save()
        logger.info(f"Skipped delivery {delivery_attempt_id}: webhook disabled")
        return

    payload = {
        'event_id': str(attempt.event.id),
        'event_type': attempt.event.event_type,
        'user_id': attempt.event.user_id,
        'payload': attempt.event.payload,
        'webhook_id': str(attempt.webhook.id),
        'timestamp': attempt.event.created_at.isoformat(),
    }

    try:
        response = requests.post(
            attempt.webhook.url,
            json=payload,
            timeout=10,
            headers={'Content-Type': 'application/json', 'X-Webhook-Event': attempt.event.event_type},
        )
        attempt.response_status_code = response.status_code
        attempt.response_body = response.text[:1000]

        if 200 <= response.status_code < 300:
            attempt.status = 'success'
            logger.info(f"Delivered {delivery_attempt_id} → {response.status_code}")
        else:
            attempt.status = 'failed'
            attempt.error_message = f"Non-2xx response: {response.status_code}"
            logger.warning(f"Failed delivery {delivery_attempt_id} → {response.status_code}")

    except requests.exceptions.Timeout:
        attempt.status = 'failed'
        attempt.error_message = 'Request timed out'
        logger.warning(f"Timeout delivering {delivery_attempt_id}")
    except requests.exceptions.ConnectionError as e:
        attempt.status = 'failed'
        attempt.error_message = f"Connection error: {str(e)}"
        logger.warning(f"Connection error delivering {delivery_attempt_id}: {e}")
    except Exception as e:
        attempt.status = 'failed'
        attempt.error_message = f"Unexpected error: {str(e)}"
        logger.error(f"Unexpected error delivering {delivery_attempt_id}: {e}")

    attempt.delivered_at = django_tz.now()
    attempt.save()
