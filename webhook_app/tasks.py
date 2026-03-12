import logging
import requests

from datetime import timezone as dt_timezone, datetime
from celery import shared_task
import redis as redis_lib
from django.conf import settings

logger = logging.getLogger(__name__)

DELIVERY_QUEUE_KEY = 'webhook:delivery_queue'
RATE_LIMIT_KEY     = 'webhook:rate_limit'


def get_redis():
    return redis_lib.from_url(settings.REDIS_URL, decode_responses=True)


def get_rate_limit() -> int:
    try:
        val = get_redis().get(RATE_LIMIT_KEY)
        if val:
            return max(1, int(val))
    except Exception:
        pass
    return getattr(settings, 'DEFAULT_RATE_LIMIT', 10)


def set_rate_limit(deliveries_per_second: int):
    get_redis().set(RATE_LIMIT_KEY, deliveries_per_second)
    try:
        from webhook_app.models import RateLimitConfig
        cfg = RateLimitConfig.get_instance()
        cfg.deliveries_per_second = deliveries_per_second
        cfg.save()
    except Exception:
        pass


def enqueue_delivery(delivery_id: str):
    get_redis().rpush(DELIVERY_QUEUE_KEY, str(delivery_id))


def get_queue_length() -> int:
    try:
        return get_redis().llen(DELIVERY_QUEUE_KEY)
    except Exception:
        return 0


@shared_task(name='webhook_app.tasks.drain_delivery_queue')
def drain_delivery_queue():
    """
    Fires every 1s via Celery Beat.
    Pops exactly `rate` ids from the Redis list and dispatches one Celery
    task per id. This is the sole rate-limiting gate — nothing else needed.
    """
    rate = get_rate_limit()
    r = get_redis()

    pipe = r.pipeline()
    for _ in range(rate):
        pipe.lpop(DELIVERY_QUEUE_KEY)
    ids = [x for x in pipe.execute() if x]

    for delivery_id in ids:
        execute_delivery.delay(delivery_id)

    remaining = get_queue_length()
    if ids or remaining:
        logger.info(f"drain: dispatched={len(ids)} rate={rate}/s remaining={remaining}")


@shared_task(name='webhook_app.tasks.execute_delivery', bind=True, max_retries=3)
def execute_delivery(self, delivery_id: str):
    from webhook_app.models import DeliveryAttempt

    try:
        attempt = DeliveryAttempt.objects.select_related('webhook', 'event').get(id=delivery_id)
    except DeliveryAttempt.DoesNotExist:
        logger.error(f"DeliveryAttempt {delivery_id} not found")
        return

    if not attempt.webhook.is_active:
        attempt.status = 'failed'
        attempt.error_message = 'Webhook disabled'
        attempt.delivered_at = datetime.now(dt_timezone.utc)
        attempt.save(update_fields=['status', 'error_message', 'delivered_at'])
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
        resp = requests.post(
            attempt.webhook.url,
            json=payload,
            timeout=10,
            headers={'Content-Type': 'application/json', 'X-Webhook-Event': attempt.event.event_type},
        )
        attempt.response_status_code = resp.status_code
        attempt.response_body = resp.text[:1000]

        if 200 <= resp.status_code < 300:
            attempt.status = 'success'
            attempt.delivered_at = datetime.now(dt_timezone.utc)
            logger.info(f"✓ {delivery_id[:8]} -> {resp.status_code}")
        else:
            attempt.status = 'failed'
            attempt.error_message = f"HTTP {resp.status_code}"

    except requests.exceptions.ConnectionError as exc:
        attempt.status = 'failed'
        attempt.error_message = f"Connection error: {exc}"
        attempt.save(update_fields=['status', 'error_message'])
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)

    except requests.exceptions.Timeout:
        attempt.status = 'failed'
        attempt.error_message = 'Timeout'

    except Exception as exc:
        attempt.status = 'failed'
        attempt.error_message = str(exc)

    attempt.save(update_fields=['status', 'response_status_code', 'response_body', 'error_message', 'delivered_at'])