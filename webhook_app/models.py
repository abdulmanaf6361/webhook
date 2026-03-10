from django.db import models
import uuid


EVENT_TYPES = [
    ('request.created', 'Request Created'),
    ('request.updated', 'Request Updated'),
    ('request.deleted', 'Request Deleted'),
]

DELIVERY_STATUS = [
    ('pending', 'Pending'),
    ('success', 'Success'),
    ('failed', 'Failed'),
]


class Webhook(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.CharField(max_length=255, db_index=True)
    url = models.URLField(max_length=2048)
    event_types = models.JSONField(default=list)  # list of subscribed event types
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Webhook({self.user_id}, {self.url}, active={self.is_active})"

    def subscribes_to(self, event_type):
        return event_type in self.event_types


class Event(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.CharField(max_length=255, db_index=True)
    event_type = models.CharField(max_length=50, choices=EVENT_TYPES)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Event({self.event_type}, user={self.user_id})"


class DeliveryAttempt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    webhook = models.ForeignKey(Webhook, on_delete=models.CASCADE, related_name='deliveries')
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='deliveries')
    status = models.CharField(max_length=20, choices=DELIVERY_STATUS, default='pending')
    response_status_code = models.IntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True, default='')
    error_message = models.TextField(blank=True, default='')
    queued_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-queued_at']

    def __str__(self):
        return f"Delivery({self.webhook_id}, {self.event.event_type}, {self.status})"


class RateLimitConfig(models.Model):
    """Singleton model storing system-wide rate limit config."""
    deliveries_per_second = models.IntegerField(default=10)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Rate Limit Config'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get_instance(cls):
        obj, _ = cls.objects.get_or_create(pk=1, defaults={'deliveries_per_second': 10})
        return obj
