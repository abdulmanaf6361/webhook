from django.contrib import admin

# Register your models here.
from .models import Webhook, Event, DeliveryAttempt, RateLimitConfig
admin.site.register(Webhook)
admin.site.register(Event)
admin.site.register(DeliveryAttempt)
admin.site.register(RateLimitConfig)