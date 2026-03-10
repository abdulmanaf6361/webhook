from django.urls import path
from . import views

urlpatterns = [
    # ── UI routes ──────────────────────────────────────────────────────────────
    path('', views.index, name='index'),
    path('webhooks/new/', views.webhook_new, name='webhook_new'),
    path('webhooks/<uuid:webhook_id>/', views.webhook_detail, name='webhook_detail'),
    path('webhooks/<uuid:webhook_id>/edit/', views.webhook_edit, name='webhook_edit'),
    path('deliveries/', views.deliveries_view, name='deliveries'),
    path('events/', views.events_view, name='events'),



    # ── Webhook CRUD API ───────────────────────────────────────────────────────
    path('api/webhooks/', views.webhooks_list_create, name='api_webhooks'),
    path('api/webhooks/<uuid:webhook_id>/', views.webhook_detail_api, name='api_webhook_detail'),
    path('api/webhooks/<uuid:webhook_id>/toggle/', views.webhook_toggle, name='api_webhook_toggle'),
    path('api/webhooks/<uuid:webhook_id>/deliveries/', views.delivery_history, name='api_delivery_history'),

    # ── Event ingestion ────────────────────────────────────────────────────────
    path('api/events/ingest/', views.ingest_event, name='api_ingest_event'),

    # ── Internal config ────────────────────────────────────────────────────────
    path('api/internal/rate-limit/', views.rate_limit_config, name='api_rate_limit'),
]