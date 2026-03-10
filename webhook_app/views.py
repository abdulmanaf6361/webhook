import logging

from django.shortcuts import render
from django.http import JsonResponse
from webhook_app.models import Webhook, EVENT_TYPES, Event, DeliveryAttempt
from django.shortcuts import get_object_or_404
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
