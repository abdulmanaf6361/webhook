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
]