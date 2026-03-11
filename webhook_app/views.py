import json
import uuid
import logging
import time

from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.views import View

from .models import Webhook, Event, DeliveryAttempt, RateLimitConfig, EVENT_TYPES
from .tasks import set_rate_limit, get_rate_limit

logger = logging.getLogger(__name__)

VALID_EVENT_TYPES = {et[0] for et in EVENT_TYPES}


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_user_id(request):
    return request.headers.get('X-User-Id') or request.POST.get('user_id') or request.GET.get('user_id', '')


def json_error(message, status=400):
    return JsonResponse({'error': message}, status=status)


def json_success(data, status=200):
    return JsonResponse(data, status=status)


# ── UI Views ───────────────────────────────────────────────────────────────────

def index(request):
    """Dashboard — lists webhooks for the given user."""
    user_id = get_user_id(request)
    status_filter = request.GET.get('status', '')
    webhooks = []
    if user_id:
        qs = Webhook.objects.filter(user_id=user_id)
        if status_filter == 'active':
            qs = qs.filter(is_active=True)
        elif status_filter == 'disabled':
            qs = qs.filter(is_active=False)
        webhooks = qs
    rate_limit = get_rate_limit()
    return render(request, 'webhook_app/index.html', {
        'webhooks': webhooks,
        'user_id': user_id,
        'status_filter': status_filter,
        'event_types': EVENT_TYPES,
        'rate_limit': rate_limit,
    })


def webhook_detail(request, webhook_id):
    """Detail view with delivery history."""
    user_id = get_user_id(request)
    webhook = get_object_or_404(Webhook, id=webhook_id)
    deliveries = DeliveryAttempt.objects.filter(webhook=webhook).select_related('event')[:50]
    return render(request, 'webhook_app/webhook_detail.html', {
        'webhook': webhook,
        'deliveries': deliveries,
        'user_id': user_id,
    })


def webhook_new(request):
    """Form to create a new webhook."""
    user_id = get_user_id(request)
    return render(request, 'webhook_app/webhook_form.html', {
        'webhook': None,
        'event_types': EVENT_TYPES,
        'user_id': user_id,
    })


def webhook_edit(request, webhook_id):
    """Form to edit an existing webhook."""
    user_id = get_user_id(request)
    webhook = get_object_or_404(Webhook, id=webhook_id)
    return render(request, 'webhook_app/webhook_form.html', {
        'webhook': webhook,
        'event_types': EVENT_TYPES,
        'user_id': user_id,
    })


def deliveries_view(request):
    """Global deliveries log for a user."""
    user_id = get_user_id(request)
    deliveries = []
    if user_id:
        deliveries = DeliveryAttempt.objects.filter(
            webhook__user_id=user_id
        ).select_related('event', 'webhook').order_by('-queued_at')[:100]
    return render(request, 'webhook_app/deliveries.html', {
        'deliveries': deliveries,
        'user_id': user_id,
    })



def test_ratelimit_view(request):
    """Rate limit Part B test page."""
    return render(request, 'webhook_app/test_ratelimit.html', {
        'user_id': get_user_id(request),
    })

def events_view(request):
    """Event publisher UI."""
    user_id = get_user_id(request)
    events = []
    if user_id:
        events = Event.objects.filter(user_id=user_id).order_by('-created_at')[:50]
    return render(request, 'webhook_app/events.html', {
        'events': events,
        'event_types': EVENT_TYPES,
        'user_id': user_id,
    })


# ── Webhook CRUD API ───────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['GET', 'POST'])
def webhooks_list_create(request):
    """
    GET  /api/webhooks/         — list webhooks for user
    POST /api/webhooks/         — create webhook
    """
    user_id = get_user_id(request)
    if not user_id:
        return json_error('X-User-Id header is required', 401)

    if request.method == 'GET':
        status_filter = request.GET.get('status', '')
        qs = Webhook.objects.filter(user_id=user_id)
        if status_filter == 'active':
            qs = qs.filter(is_active=True)
        elif status_filter == 'disabled':
            qs = qs.filter(is_active=False)
        data = [_webhook_to_dict(w) for w in qs]
        return json_success({'webhooks': data})

    # POST — create
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        # Support form submissions too
        body = request.POST.dict()
        body['event_types'] = request.POST.getlist('event_types')

    url = body.get('url', '').strip()
    event_types = body.get('event_types', [])

    if not url:
        return json_error('url is required')
    if not event_types:
        return json_error('at least one event_type is required')

    invalid = [et for et in event_types if et not in VALID_EVENT_TYPES]
    if invalid:
        return json_error(f'Invalid event types: {invalid}. Valid: {list(VALID_EVENT_TYPES)}')

    webhook = Webhook.objects.create(
        user_id=user_id,
        url=url,
        event_types=list(set(event_types)),
        is_active=True,
    )
    logger.info(f"Created webhook {webhook.id} for user {user_id}")
    return json_success(_webhook_to_dict(webhook), status=201)


@csrf_exempt
@require_http_methods(['GET', 'PUT', 'PATCH', 'DELETE', 'POST'])
def webhook_detail_api(request, webhook_id):
    """
    GET    /api/webhooks/<id>/  — get webhook
    PUT    /api/webhooks/<id>/  — full update
    PATCH  /api/webhooks/<id>/  — partial update
    DELETE /api/webhooks/<id>/  — delete
    """
    user_id = get_user_id(request)
    if not user_id:
        return json_error('X-User-Id header is required', 401)

    webhook = get_object_or_404(Webhook, id=webhook_id, user_id=user_id)

    if request.method == 'GET':
        return json_success(_webhook_to_dict(webhook))

    if request.method == 'DELETE':
        webhook.delete()
        return json_success({'message': 'Webhook deleted'})

    # PUT / PATCH / POST (form fallback)
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        body = request.POST.dict()
        if 'event_types' in request.POST:
            body['event_types'] = request.POST.getlist('event_types')

    if 'url' in body:
        webhook.url = body['url'].strip()
    if 'event_types' in body:
        event_types = body['event_types']
        invalid = [et for et in event_types if et not in VALID_EVENT_TYPES]
        if invalid:
            return json_error(f'Invalid event types: {invalid}')
        webhook.event_types = list(set(event_types))
    if 'is_active' in body:
        webhook.is_active = bool(body['is_active'])

    webhook.save()
    return json_success(_webhook_to_dict(webhook))


@csrf_exempt
@require_http_methods(['POST'])
def webhook_toggle(request, webhook_id):
    """POST /api/webhooks/<id>/toggle/ — enable/disable webhook."""
    user_id = get_user_id(request)
    if not user_id:
        return json_error('X-User-Id header is required', 401)

    webhook = get_object_or_404(Webhook, id=webhook_id, user_id=user_id)
    webhook.is_active = not webhook.is_active
    webhook.save()
    action = 'enabled' if webhook.is_active else 'disabled'
    logger.info(f"Webhook {webhook_id} {action} by user {user_id}")
    return json_success({'message': f'Webhook {action}', 'is_active': webhook.is_active})


# ── Event Ingestion API ────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['POST'])
def ingest_event(request):
    """
    POST /api/events/ingest/
    Zero-DB-write hot path — returns in <5ms.

    Flow:
      1. Validate request (no DB)
      2. Look up matching active webhooks for this user (one indexed DB read)
      3. Generate UUIDs for event + each delivery attempt
      4. Push one message per (event x webhook) onto Redis INGEST_QUEUE_KEY
      5. Return immediately

    flush_ingest_queue (Beat, every 2s) then bulk-creates Event +
    DeliveryAttempt rows and enqueues deliveries into fair per-user queues.

    The webhook lookup (step 2) is a fast indexed read — we need it here to
    know how many deliveries to generate and to get webhook IDs/URLs without
    a later DB round-trip from the worker.
    """
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return json_error('Invalid JSON body')

    user_id    = body.get('user_id',    '').strip()
    event_type = body.get('event_type', '').strip()
    payload    = body.get('payload',    {})

    if not user_id:
        return json_error('user_id is required')
    if not event_type:
        return json_error('event_type is required')
    if event_type not in VALID_EVENT_TYPES:
        return json_error(f'Invalid event_type. Valid: {list(VALID_EVENT_TYPES)}')
    if not isinstance(payload, dict):
        return json_error('payload must be a JSON object')

    # One indexed read — only active webhooks for this user
    matching_webhooks = [
        wh for wh in Webhook.objects.filter(user_id=user_id, is_active=True)
        if wh.subscribes_to(event_type)
    ]

    event_id       = str(uuid.uuid4())
    delivery_count = 0

    from .tasks import push_ingest_message
    for webhook in matching_webhooks:
        delivery_id = str(uuid.uuid4())
        push_ingest_message(
            event_id=event_id,
            user_id=user_id,
            event_type=event_type,
            payload=payload,
            webhook_id=str(webhook.id),
            delivery_id=delivery_id,
        )
        delivery_count += 1

    logger.info(
        f"Ingest queued: event={event_id} type={event_type} "
        f"user={user_id} fanout={delivery_count}"
    )
    return json_success({
        'event_id':         event_id,
        'event_type':       event_type,
        'user_id':          user_id,
        'deliveries_queued': delivery_count,
        'note':             'persisted async — appears in DB within ~2s',
    }, status=201)


# ── Rate Limit Config API ──────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['GET', 'PUT', 'POST'])
def rate_limit_config(request):
    """
    GET  /api/internal/rate-limit/  — view current rate limit
    PUT  /api/internal/rate-limit/  — update rate limit
    """
    if request.method == 'GET':
        current = get_rate_limit()
        return json_success({'deliveries_per_second': current})

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        body = request.POST.dict()

    dps = body.get('deliveries_per_second')
    if dps is None:
        return json_error('deliveries_per_second is required')
    try:
        dps = int(dps)
        if dps < 1:
            raise ValueError()
    except (TypeError, ValueError):
        return json_error('deliveries_per_second must be a positive integer')

    set_rate_limit(dps)
    logger.info(f"Rate limit updated to {dps}/s")
    return json_success({'deliveries_per_second': dps, 'message': 'Rate limit updated'})


# ── Delivery History API ───────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['GET'])
def delivery_history(request, webhook_id):
    """GET /api/webhooks/<id>/deliveries/ — delivery history for a webhook."""
    user_id = get_user_id(request)
    if not user_id:
        return json_error('X-User-Id header is required', 401)

    webhook = get_object_or_404(Webhook, id=webhook_id, user_id=user_id)
    deliveries = DeliveryAttempt.objects.filter(webhook=webhook).select_related('event').order_by('-queued_at')[:50]
    return json_success({'deliveries': [_delivery_to_dict(d) for d in deliveries]})


# ── Serializers ────────────────────────────────────────────────────────────────

def _webhook_to_dict(w: Webhook) -> dict:
    return {
        'id': str(w.id),
        'user_id': w.user_id,
        'url': w.url,
        'event_types': w.event_types,
        'is_active': w.is_active,
        'created_at': w.created_at.isoformat(),
        'updated_at': w.updated_at.isoformat(),
    }


def _delivery_to_dict(d: DeliveryAttempt) -> dict:
    return {
        'id': str(d.id),
        'webhook_id': str(d.webhook_id),
        'event_id': str(d.event_id),
        'event_type': d.event.event_type,
        'status': d.status,
        'response_status_code': d.response_status_code,
        'error_message': d.error_message,
        'queued_at': d.queued_at.isoformat(),
        'delivered_at': d.delivered_at.isoformat() if d.delivered_at else None,
    }


# ── Rate Limit Test UI + SSE runner ───────────────────────────────────────────

def test_page(request):
    """Render the Part B rate limit test page."""
    return render(request, 'webhook_app/test.html', {
        'user_id': get_user_id(request),
    })


import threading
import queue as queue_module

@csrf_exempt
@require_http_methods(['GET'])
def test_run_sse(request):
    """
    GET /api/test/run/?rate1=5&rate2=20&count=20&register=1
    Streams test progress as Server-Sent Events so the UI can show live logs.
    Each SSE message is: data: <json>\n\n
    """
    import concurrent.futures

    rate1    = int(request.GET.get('rate1', 5))
    rate2    = int(request.GET.get('rate2', 20))
    count    = int(request.GET.get('count', 20))
    register = request.GET.get('register', '0') == '1'
    user_id  = request.GET.get('user_id', 'test_user_ratelimit').strip() or 'test_user_ratelimit'
    webhook_url = request.GET.get('webhook_url', 'http://mock_receiver:9000/').strip()

    log_q = queue_module.Queue()

    def log(msg, kind='info'):
        log_q.put({'msg': msg, 'kind': kind})

    def publish_burst(n, offset=0):
        errors = []
        results = []
        lock = threading.Lock()

        def _publish(i):
            import urllib.request as ur, urllib.error as ue
            body = json.dumps({
                'user_id': user_id,
                'event_type': 'request.created',
                'payload': {'index': offset + i},
            }).encode()
            req_obj = ur.Request(
                request.build_absolute_uri('/api/events/ingest/'),
                data=body,
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            try:
                with ur.urlopen(req_obj, timeout=15) as r:
                    with lock:
                        results.append(json.loads(r.read()))
            except Exception as e:
                with lock:
                    errors.append(str(e))

        threads = [threading.Thread(target=_publish, args=(i,)) for i in range(n)]
        t0 = time.time()
        for t in threads: t.start()
        for t in threads: t.join()
        return time.time() - t0, errors

    def get_user_deliveries():
        """Count successful+failed deliveries for our test user in the DB."""
        from .models import DeliveryAttempt
        return DeliveryAttempt.objects.filter(
            event__user_id=user_id,
            event__event_type='request.created',
        ).exclude(status='pending')

    def wait_deliveries(baseline, expected, timeout=45):
        """Poll DB until expected new non-pending deliveries appear."""
        deadline = time.time() + timeout
        last_seen = baseline
        while time.time() < deadline:
            now = get_user_deliveries().count()
            if now > last_seen:
                last_seen = now
                log(f"  deliveries so far: {now - baseline}/{expected}", 'progress')
            if now >= baseline + expected:
                return now - baseline, time.time()
            time.sleep(0.6)
        return get_user_deliveries().count() - baseline, time.time()

    def _run_test():
        import time as t_mod
        import urllib.request as ur

        try:
            # ── Pre-flight ────────────────────────────────────────────────────
            log('━━━ Pre-flight ━━━', 'section')
            try:
                with ur.urlopen(
                    request.build_absolute_uri('/api/internal/rate-limit/'), timeout=5
                ) as r:
                    data = json.loads(r.read())
                log(f"✓ API reachable — current rate: {data['deliveries_per_second']}/s", 'success')
            except Exception as e:
                log(f"✗ Cannot reach API: {e}", 'error')
                log_q.put({'done': True, 'passed': False})
                return

            # ── Register webhook ──────────────────────────────────────────────
            if register:
                log('━━━ Registering webhook ━━━', 'section')
                body = json.dumps({
                    'url': webhook_url,
                    'event_types': ['request.created'],
                }).encode()
                req_obj = ur.Request(
                    request.build_absolute_uri('/api/webhooks/'),
                    data=body,
                    headers={'Content-Type': 'application/json', 'X-User-Id': user_id},
                    method='POST',
                )
                try:
                    with ur.urlopen(req_obj, timeout=10) as r:
                        wdata = json.loads(r.read())
                    log(f"✓ Registered webhook {str(wdata['id'])[:18]}…", 'success')
                except Exception as e:
                    log(f"✗ Failed to register webhook: {e}", 'error')
                    log_q.put({'done': True, 'passed': False})
                    return

            # ── Batch 1: rate1/s ──────────────────────────────────────────────
            log(f'━━━ Batch 1 — rate limit: {rate1}/s ━━━', 'section')

            # Set rate
            rl_body = json.dumps({'deliveries_per_second': rate1}).encode()
            rl_req = ur.Request(
                request.build_absolute_uri('/api/internal/rate-limit/'),
                data=rl_body,
                headers={'Content-Type': 'application/json'},
                method='PUT',
            )
            with ur.urlopen(rl_req, timeout=5): pass
            log(f"✓ Rate limit set to {rate1}/s", 'success')
            log(f"⏳ Waiting 1.5s for token bucket to reset…", 'info')
            t_mod.sleep(1.5)

            baseline1 = get_user_deliveries().count()
            log(f"📤 Publishing {count} events concurrently…", 'info')
            pub_t1, errors1 = publish_burst(count, offset=0)
            if errors1:
                log(f"✗ {len(errors1)} publish errors: {errors1[0]}", 'error')
                log_q.put({'done': True, 'passed': False})
                return
            log(f"✓ All {count} published in {pub_t1:.2f}s", 'success')

            log(f"⏳ Waiting for deliveries at {rate1}/s…", 'info')
            start1 = t_mod.time()
            delivered1, end1 = wait_deliveries(baseline1, count)
            spread1 = end1 - start1

            log(f"✓ Delivered: {delivered1}/{count}", 'success' if delivered1 == count else 'error')
            log(f"✓ Spread: {spread1:.1f}s  (expected ≥ {count/rate1:.0f}s theoretical)", 'success' if spread1 >= 2 else 'warn')

            # ── Update rate live ──────────────────────────────────────────────
            log(f'━━━ Updating rate limit live → {rate2}/s ━━━', 'section')
            rl_body2 = json.dumps({'deliveries_per_second': rate2}).encode()
            rl_req2 = ur.Request(
                request.build_absolute_uri('/api/internal/rate-limit/'),
                data=rl_body2,
                headers={'Content-Type': 'application/json'},
                method='PUT',
            )
            with ur.urlopen(rl_req2, timeout=5): pass
            log(f"✓ Rate limit updated to {rate2}/s (no restart)", 'success')
            t_mod.sleep(1.5)

            # ── Batch 2: rate2/s ──────────────────────────────────────────────
            log(f'━━━ Batch 2 — rate limit: {rate2}/s ━━━', 'section')
            baseline2 = get_user_deliveries().count()
            log(f"📤 Publishing {count} events concurrently…", 'info')
            pub_t2, errors2 = publish_burst(count, offset=count)
            if errors2:
                log(f"✗ {len(errors2)} publish errors", 'error')
                log_q.put({'done': True, 'passed': False})
                return
            log(f"✓ All {count} published in {pub_t2:.2f}s", 'success')

            log(f"⏳ Waiting for deliveries at {rate2}/s…", 'info')
            start2 = t_mod.time()
            delivered2, end2 = wait_deliveries(baseline2, count)
            spread2 = end2 - start2

            log(f"✓ Delivered: {delivered2}/{count}", 'success' if delivered2 == count else 'error')
            log(f"✓ Spread: {spread2:.1f}s  (expected ≤ {count/rate2:.1f}s)", 'success' if spread2 <= spread1 * 0.7 else 'warn')

            # ── Results ───────────────────────────────────────────────────────
            total = delivered1 + delivered2
            passed = (
                delivered1 == count and
                delivered2 == count and
                spread1 >= 2.0 and
                spread2 < spread1 * 0.7
            )
            log('━━━ Final Results ━━━', 'section')
            log(f"Batch 1 ({rate1}/s): {delivered1}/{count} delivered | spread {spread1:.1f}s", 'success' if delivered1 == count else 'error')
            log(f"Batch 2 ({rate2}/s): {delivered2}/{count} delivered | spread {spread2:.1f}s", 'success' if delivered2 == count else 'error')
            log(f"Total: {total}/{count*2} — {'NO LOSSES ✓' if total == count*2 else 'LOSSES DETECTED ✗'}", 'success' if total == count*2 else 'error')

            log_q.put({
                'done': True,
                'passed': passed,
                'stats': {
                    'batch1': {'delivered': delivered1, 'spread': round(spread1, 1), 'rate': rate1},
                    'batch2': {'delivered': delivered2, 'spread': round(spread2, 1), 'rate': rate2},
                    'total': total,
                    'expected': count * 2,
                }
            })

        except Exception as e:
            log(f"✗ Unexpected error: {e}", 'error')
            log_q.put({'done': True, 'passed': False})

    # Start test in background thread
    t = threading.Thread(target=_run_test, daemon=True)
    t.start()

    # Stream SSE responses
    def event_stream():
        import time as t_mod
        while True:
            try:
                item = log_q.get(timeout=60)
                yield f"data: {json.dumps(item)}\n\n"
                if item.get('done'):
                    break
            except queue_module.Empty:
                yield "data: {\"msg\": \"timeout\", \"kind\": \"error\"}\n\n"
                break

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


# ── Proxy: mock receiver logs ──────────────────────────────────────────────────
import urllib.request as _urllib_req
import os as _os

@csrf_exempt
@require_http_methods(['GET'])
def proxy_receiver_logs(request):
    """
    GET /api/internal/receiver-logs/?user_id=xxx
    Server-side proxy to mock_receiver so the browser doesn't need
    direct access to the Docker-internal hostname.
    """
    receiver_host = _os.environ.get('MOCK_RECEIVER_URL', 'http://mock_receiver:9000')
    try:
        with _urllib_req.urlopen(f"{receiver_host}/logs", timeout=5) as r:
            data = json.loads(r.read())
        logs = data.get('logs', [])
    except Exception as e:
        return json_error(f"Cannot reach mock receiver: {e}", 502)

    user_id = request.GET.get('user_id', '')
    if user_id:
        logs = [l for l in logs if l.get('body', {}).get('user_id') == user_id]

    return json_success({'count': len(logs), 'logs': logs})


@csrf_exempt
@require_http_methods(['GET'])
def delivery_count(request):
    """
    GET /api/internal/delivery-count/?user_id=xxx&since=<iso>
    Returns count of successful DeliveryAttempts for a user.
    Used by UI test to poll delivery progress via Django DB
    (avoids needing browser access to mock_receiver directly).
    """
    user_id = request.GET.get('user_id', '').strip()
    since   = request.GET.get('since', '')
    if not user_id:
        return json_error('user_id is required')

    qs = DeliveryAttempt.objects.filter(
        webhook__user_id=user_id,
        status='success',
    )
    if since:
        try:
            from django.utils.dateparse import parse_datetime
            since_dt = parse_datetime(since)
            if since_dt:
                qs = qs.filter(delivered_at__gte=since_dt)
        except Exception:
            pass

    return json_success({'count': qs.count(), 'user_id': user_id})