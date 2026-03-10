import logging

from django.shortcuts import render
from django.http import JsonResponse
from webhook_app.models import Webhook, EVENT_TYPES
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