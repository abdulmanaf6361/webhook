import json
import logging
import urllib.request as _urllib_req
import os as _os

from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt

from .models import Webhook, Event, DeliveryAttempt, RateLimitConfig, EVENT_TYPES
from .tasks import set_rate_limit, get_rate_limit, enqueue_delivery

logger = logging.getLogger(__name__)
VALID_EVENT_TYPES = {et[0] for et in EVENT_TYPES}


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_user_id(request):
    return request.headers.get('X-User-Id') or request.POST.get('user_id') or request.GET.get('user_id', '')

def json_error(msg, status=400):
    return JsonResponse({'error': msg}, status=status)

def json_success(data, status=200):
    return JsonResponse(data, status=status)


# ── UI Pages ───────────────────────────────────────────────────────────────────

def index(request):
    user_id = get_user_id(request)
    webhooks = []
    if user_id:
        webhooks = Webhook.objects.filter(user_id=user_id)
    return render(request, 'webhook_app/index.html', {
        'webhooks': webhooks, 'user_id': user_id,
        'event_types': EVENT_TYPES, 'rate_limit': get_rate_limit(),
    })

def webhook_detail(request, webhook_id):
    user_id = get_user_id(request)
    webhook = get_object_or_404(Webhook, id=webhook_id)
    deliveries = DeliveryAttempt.objects.filter(webhook=webhook).select_related('event')[:50]
    return render(request, 'webhook_app/webhook_detail.html', {
        'webhook': webhook, 'deliveries': deliveries, 'user_id': user_id,
    })

def webhook_new(request):
    return render(request, 'webhook_app/webhook_form.html', {
        'webhook': None, 'event_types': EVENT_TYPES, 'user_id': get_user_id(request),
    })

def webhook_edit(request, webhook_id):
    webhook = get_object_or_404(Webhook, id=webhook_id)
    return render(request, 'webhook_app/webhook_form.html', {
        'webhook': webhook, 'event_types': EVENT_TYPES, 'user_id': get_user_id(request),
    })

def deliveries_view(request):
    user_id = get_user_id(request)
    deliveries = []
    if user_id:
        deliveries = DeliveryAttempt.objects.filter(
            webhook__user_id=user_id
        ).select_related('event', 'webhook').order_by('-queued_at')[:100]
    return render(request, 'webhook_app/deliveries.html', {'deliveries': deliveries, 'user_id': user_id})

def events_view(request):
    user_id = get_user_id(request)
    events = []
    if user_id:
        events = Event.objects.filter(user_id=user_id).order_by('-created_at')[:50]
    return render(request, 'webhook_app/events.html', {
        'events': events, 'event_types': EVENT_TYPES, 'user_id': user_id,
    })

def test_page(request):
    return render(request, 'webhook_app/test.html', {'user_id': get_user_id(request)})

def test_ratelimit_view(request):
    """Part B rate limit test page at /test/rate-limit/"""
    response = render(request, 'webhook_app/test_ratelimit.html', {'user_id': get_user_id(request)})
    response['Cache-Control'] = 'no-store'
    return response


# ── Webhook CRUD API ───────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['GET', 'POST'])
def webhooks_list_create(request):
    user_id = get_user_id(request)
    if not user_id:
        return json_error('X-User-Id header required', 401)

    if request.method == 'GET':
        qs = Webhook.objects.filter(user_id=user_id)
        status_filter = request.GET.get('status', '')
        if status_filter == 'active':
            qs = qs.filter(is_active=True)
        elif status_filter == 'disabled':
            qs = qs.filter(is_active=False)
        return json_success({'webhooks': [_webhook_dict(w) for w in qs]})

    try:
        body = json.loads(request.body)
    except Exception:
        body = request.POST.dict()
        body['event_types'] = request.POST.getlist('event_types')

    url = body.get('url', '').strip()
    event_types = body.get('event_types', [])
    if not url:
        return json_error('url is required')
    if not event_types:
        return json_error('at least one event_type is required')
    invalid = [e for e in event_types if e not in VALID_EVENT_TYPES]
    if invalid:
        return json_error(f'Invalid event types: {invalid}')

    webhook = Webhook.objects.create(user_id=user_id, url=url, event_types=list(set(event_types)), is_active=True)
    return json_success(_webhook_dict(webhook), status=201)


@csrf_exempt
@require_http_methods(['GET', 'PUT', 'PATCH', 'DELETE', 'POST'])
def webhook_detail_api(request, webhook_id):
    user_id = get_user_id(request)
    if not user_id:
        return json_error('X-User-Id header required', 401)

    webhook = get_object_or_404(Webhook, id=webhook_id, user_id=user_id)

    if request.method == 'GET':
        return json_success(_webhook_dict(webhook))
    if request.method == 'DELETE':
        webhook.delete()
        return json_success({'message': 'Webhook deleted'})

    try:
        body = json.loads(request.body)
    except Exception:
        body = request.POST.dict()
        if 'event_types' in request.POST:
            body['event_types'] = request.POST.getlist('event_types')

    if 'url' in body:
        webhook.url = body['url'].strip()
    if 'event_types' in body:
        invalid = [e for e in body['event_types'] if e not in VALID_EVENT_TYPES]
        if invalid:
            return json_error(f'Invalid event types: {invalid}')
        webhook.event_types = list(set(body['event_types']))
    if 'is_active' in body:
        webhook.is_active = bool(body['is_active'])
    webhook.save()
    return json_success(_webhook_dict(webhook))


@csrf_exempt
@require_http_methods(['POST'])
def webhook_toggle(request, webhook_id):
    user_id = get_user_id(request)
    if not user_id:
        return json_error('X-User-Id header required', 401)
    webhook = get_object_or_404(Webhook, id=webhook_id, user_id=user_id)
    webhook.is_active = not webhook.is_active
    webhook.save()
    return json_success({'is_active': webhook.is_active})


# ── Event Ingestion ────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['POST'])
def ingest_event(request):
    """
    POST /api/events/ingest/
    Writes Event + DeliveryAttempts to DB, pushes ids to Redis queue.
    Beat picks up `rate` ids per second and dispatches execute_delivery tasks.
    """
    try:
        body = json.loads(request.body)
    except Exception:
        return json_error('Invalid JSON')

    user_id    = body.get('user_id', '').strip()
    event_type = body.get('event_type', '').strip()
    payload    = body.get('payload', {})

    if not user_id:
        return json_error('user_id required')
    if not event_type:
        return json_error('event_type required')
    if event_type not in VALID_EVENT_TYPES:
        return json_error(f'Invalid event_type. Valid: {list(VALID_EVENT_TYPES)}')
    if not isinstance(payload, dict):
        return json_error('payload must be a JSON object')

    matching = [w for w in Webhook.objects.filter(user_id=user_id, is_active=True)
                if w.subscribes_to(event_type)]

    event = Event.objects.create(user_id=user_id, event_type=event_type, payload=payload)

    count = 0
    for webhook in matching:
        attempt = DeliveryAttempt.objects.create(webhook=webhook, event=event, status='pending')
        enqueue_delivery(str(attempt.id))
        count += 1

    return json_success({'event_id': str(event.id), 'deliveries_queued': count}, status=201)


# ── Rate Limit Config ──────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['GET', 'PUT', 'POST'])
def rate_limit_config(request):
    if request.method == 'GET':
        return json_success({'deliveries_per_second': get_rate_limit()})

    try:
        body = json.loads(request.body)
    except Exception:
        body = request.POST.dict()

    dps = body.get('deliveries_per_second')
    try:
        dps = int(dps)
        if dps < 1:
            raise ValueError()
    except (TypeError, ValueError):
        return json_error('deliveries_per_second must be a positive integer')

    set_rate_limit(dps)
    return json_success({'deliveries_per_second': dps, 'message': 'Updated'})


# ── Delivery History ───────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['GET'])
def delivery_history(request, webhook_id):
    user_id = get_user_id(request)
    if not user_id:
        return json_error('X-User-Id header required', 401)
    webhook = get_object_or_404(Webhook, id=webhook_id, user_id=user_id)
    deliveries = DeliveryAttempt.objects.filter(webhook=webhook).select_related('event').order_by('-queued_at')[:50]
    return json_success({'deliveries': [_delivery_dict(d) for d in deliveries]})


# ── Receiver proxy & delivery count ───────────────────────────────────────────

@csrf_exempt
@require_http_methods(['GET'])
def proxy_receiver_logs(request):
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
    user_id = request.GET.get('user_id', '').strip()
    if not user_id:
        return json_error('user_id required')
    count = DeliveryAttempt.objects.filter(
        webhook__user_id=user_id
    ).exclude(status='pending').count()
    return json_success({'count': count, 'user_id': user_id})


# ── Serialisers ────────────────────────────────────────────────────────────────

def _webhook_dict(w):
    return {
        'id': str(w.id), 'user_id': w.user_id, 'url': w.url,
        'event_types': w.event_types, 'is_active': w.is_active,
        'created_at': w.created_at.isoformat(), 'updated_at': w.updated_at.isoformat(),
    }

def _delivery_dict(d):
    return {
        'id': str(d.id), 'webhook_id': str(d.webhook_id), 'event_id': str(d.event_id),
        'event_type': d.event.event_type, 'status': d.status,
        'response_status_code': d.response_status_code,
        'error_message': d.error_message,
        'queued_at': d.queued_at.isoformat(),
        'delivered_at': d.delivered_at.isoformat() if d.delivered_at else None,
    }


# ── SSE test runner (used by /test/) ──────────────────────────────────────────
import threading
import queue as _queue_mod
import time as _time

@csrf_exempt
@require_http_methods(['GET'])
def test_run_sse(request):
    """
    GET /api/test/run/?rate1=5&rate2=20&count=20&register=1
    Streams Server-Sent Events so test.html can show live progress.
    """
    from django.http import StreamingHttpResponse

    rate1    = int(request.GET.get('rate1', 5))
    rate2    = int(request.GET.get('rate2', 20))
    count    = int(request.GET.get('count', 20))
    register = request.GET.get('register', '0') == '1'
    user_id  = request.GET.get('user_id', 'test_user_ratelimit').strip()
    wh_url   = request.GET.get('webhook_url', 'http://mock_receiver:9000/').strip()

    q = _queue_mod.Queue()

    def log(msg, kind='info'):
        q.put({'msg': msg, 'kind': kind})

    def count_delivered(since_iso):
        import urllib.request as ur
        try:
            url = f"http://127.0.0.1:{request.META.get('SERVER_PORT','8000')}/api/internal/receiver-logs/?user_id={user_id}"
            with ur.urlopen(url, timeout=5) as r:
                data = json.loads(r.read())
            logs = data.get('logs', [])
            if not since_iso:
                return len(logs)
            since_ms = _time.mktime(_time.strptime(since_iso[:19], '%Y-%m-%dT%H:%M:%S')) * 1000
            return sum(1 for l in logs if _time.mktime(_time.strptime(l['received_at'][:19], '%Y-%m-%dT%H:%M:%S')) * 1000 >= since_ms)
        except Exception:
            return 0

    def publish_one(idx, offset):
        import urllib.request as ur
        body = json.dumps({'user_id': user_id, 'event_type': 'request.created', 'payload': {'i': offset+idx}}).encode()
        req = ur.Request(
            f"http://127.0.0.1:{request.META.get('SERVER_PORT','8000')}/api/events/ingest/",
            data=body, headers={'Content-Type': 'application/json'}, method='POST'
        )
        try:
            with ur.urlopen(req, timeout=15): return True
        except Exception:
            return False

    def burst(n, offset):
        threads = []
        results = []
        lock = threading.Lock()
        def _do(i):
            ok = publish_one(i, offset)
            with lock: results.append(ok)
        for i in range(n):
            t = threading.Thread(target=_do, args=(i,))
            threads.append(t)
            t.start()
        t0 = _time.time()
        for t in threads: t.join()
        return sum(results), _time.time() - t0

    def wait_deliveries(since_iso, expected, rate):
        timeout = max((expected / rate) * 1000, 3000) / 1000 + 15
        deadline = _time.time() + timeout
        seen = 0; first = None; last = None; t0 = _time.time()
        while _time.time() < deadline:
            n = count_delivered(since_iso)
            if n > seen:
                if first is None: first = _time.time() - t0
                last = _time.time() - t0
                seen = n
                log(f"  deliveries so far: {n}/{expected}", 'progress')
            if seen >= expected: break
            _time.sleep(0.6)
        spread = (last - first) if (first is not None and last is not None) else 0
        return seen, spread

    def _run():
        import urllib.request as ur

        try:
            # Pre-flight
            log('━━━ Pre-flight ━━━', 'section')
            try:
                with ur.urlopen(f"http://127.0.0.1:{request.META.get('SERVER_PORT','8000')}/api/internal/rate-limit/", timeout=5) as r:
                    d = json.loads(r.read())
                log(f"✓ API reachable — rate: {d['deliveries_per_second']}/s", 'success')
            except Exception as e:
                log(f"✗ API not reachable: {e}", 'error')
                q.put({'done': True, 'passed': False}); return

            # Register webhook
            if register:
                log('━━━ Registering webhook ━━━', 'section')
                body = json.dumps({'url': wh_url, 'event_types': ['request.created']}).encode()
                req = ur.Request(
                    f"http://127.0.0.1:{request.META.get('SERVER_PORT','8000')}/api/webhooks/",
                    data=body, headers={'Content-Type':'application/json','X-User-Id':user_id}, method='POST'
                )
                try:
                    with ur.urlopen(req, timeout=10) as r:
                        wd = json.loads(r.read())
                    log(f"✓ Webhook: {str(wd['id'])[:16]}…", 'success')
                except Exception as e:
                    log(f"✗ Failed: {e}", 'error')
                    q.put({'done': True, 'passed': False}); return

            # Batch 1
            log(f'━━━ Batch 1 — {rate1}/s ━━━', 'section')
            rl_body = json.dumps({'deliveries_per_second': rate1}).encode()
            ur.urlopen(ur.Request(
                f"http://127.0.0.1:{request.META.get('SERVER_PORT','8000')}/api/internal/rate-limit/",
                data=rl_body, headers={'Content-Type':'application/json'}, method='PUT'
            ), timeout=5)
            log(f"✓ Rate → {rate1}/s", 'success')
            _time.sleep(1.5)

            since1 = _time.strftime('%Y-%m-%dT%H:%M:%S')
            ok1, pub_t1 = burst(count, 0)
            log(f"✓ {ok1}/{count} published in {pub_t1:.2f}s", 'success')

            delivered1, spread1 = wait_deliveries(since1, count, rate1)
            log(f"Batch 1: {delivered1}/{count} | spread {spread1:.1f}s", 'success' if delivered1 == count else 'error')

            # Update rate
            log(f'━━━ Update rate → {rate2}/s ━━━', 'section')
            rl_body2 = json.dumps({'deliveries_per_second': rate2}).encode()
            ur.urlopen(ur.Request(
                f"http://127.0.0.1:{request.META.get('SERVER_PORT','8000')}/api/internal/rate-limit/",
                data=rl_body2, headers={'Content-Type':'application/json'}, method='PUT'
            ), timeout=5)
            log(f"✓ Rate → {rate2}/s", 'success')
            _time.sleep(1.5)

            # Batch 2
            log(f'━━━ Batch 2 — {rate2}/s ━━━', 'section')
            since2 = _time.strftime('%Y-%m-%dT%H:%M:%S')
            ok2, pub_t2 = burst(count, count)
            log(f"✓ {ok2}/{count} published in {pub_t2:.2f}s", 'success')

            delivered2, spread2 = wait_deliveries(since2, count, rate2)
            log(f"Batch 2: {delivered2}/{count} | spread {spread2:.1f}s", 'success' if delivered2 == count else 'error')

            total  = delivered1 + delivered2
            passed = delivered1 == count and delivered2 == count and spread2 < spread1 * 0.7

            if spread2 < spread1 * 0.7:
                log(f"✓ Arrived much faster: {spread2:.1f}s vs {spread1:.1f}s", 'success')
            if total == count * 2:
                log(f"✓ NO LOSSES: {total}/{count*2}", 'success')
            else:
                log(f"✗ LOSSES: {total}/{count*2}", 'error')

            q.put({
                'done': True, 'passed': passed,
                'stats': {
                    'batch1': {'delivered': delivered1, 'spread': round(spread1,1), 'rate': rate1},
                    'batch2': {'delivered': delivered2, 'spread': round(spread2,1), 'rate': rate2},
                    'total': total, 'expected': count * 2,
                }
            })
        except Exception as e:
            log(f"✗ Error: {e}", 'error')
            q.put({'done': True, 'passed': False})

    threading.Thread(target=_run, daemon=True).start()

    def stream():
        while True:
            try:
                item = q.get(timeout=90)
                yield f"data: {json.dumps(item)}\n\n"
                if item.get('done'):
                    break
            except _queue_mod.Empty:
                yield 'data: {"msg":"timeout","kind":"error"}\n\n'
                break

    from django.http import StreamingHttpResponse
    resp = StreamingHttpResponse(stream(), content_type='text/event-stream')
    resp['Cache-Control'] = 'no-cache'
    resp['X-Accel-Buffering'] = 'no'
    return resp