import logging
import json
from django.shortcuts import render
from django.http import JsonResponse
from webhook_app.models import Webhook, EVENT_TYPES, Event, DeliveryAttempt
from django.shortcuts import get_object_or_404
from .tasks import set_rate_limit, get_rate_limit
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
logger = logging.getLogger(__name__)


VALID_EVENT_TYPES = {et[0] for et in EVENT_TYPES}

# ── Helpers ────────────────────────────────────────────────────────────────────

def get_user_id(request):
    return request.headers.get('X-User-Id') or request.POST.get('user_id') or request.GET.get('user_id', '')


def json_error(message, status=400):
    return JsonResponse({'error': message}, status=status)


def json_success(data, status=200):
    return JsonResponse(data, status=status)



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
    Internal endpoint — accepts an event and fans out deliveries.
    Body: { user_id, event_type, payload }
    """
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return json_error('Invalid JSON body')

    user_id = body.get('user_id', '').strip()
    event_type = body.get('event_type', '').strip()
    payload = body.get('payload', {})

    if not user_id:
        return json_error('user_id is required')
    if not event_type:
        return json_error('event_type is required')
    if event_type not in VALID_EVENT_TYPES:
        return json_error(f'Invalid event_type. Valid: {list(VALID_EVENT_TYPES)}')
    if not isinstance(payload, dict):
        return json_error('payload must be a JSON object')

    # Persist event
    event = Event.objects.create(user_id=user_id, event_type=event_type, payload=payload)

    # Fan out to all active webhooks subscribed to this event type
    matching_webhooks = Webhook.objects.filter(
        user_id=user_id,
        is_active=True,
    )
    delivery_count = 0
    for webhook in matching_webhooks:
        if webhook.subscribes_to(event_type):
            attempt = DeliveryAttempt.objects.create(webhook=webhook, event=event, status='pending')
            # Enqueue delivery task
            from .tasks import deliver_webhook
            deliver_webhook.delay(str(attempt.id))
            delivery_count += 1

    logger.info(f"Event {event.id} ({event_type}) ingested for user {user_id}, fanning out to {delivery_count} webhooks")
    return json_success({
        'event_id': str(event.id),
        'event_type': event_type,
        'user_id': user_id,
        'deliveries_queued': delivery_count,
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

