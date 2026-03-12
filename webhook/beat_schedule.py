# webhooks/beat_schedule.py
from datetime import timedelta

# Periodic task: drain the fair delivery queue every second
CELERYBEAT_SCHEDULE = {
    'process-delivery-queue': {
        'task': 'webhook_app.tasks.process_delivery_queue',
        'schedule': timedelta(seconds=1),
    },
}
