#!/bin/bash
set -e

echo "Waiting for Redis..."
until python -c "import redis; r=redis.from_url('${REDIS_URL:-redis://redis:6379/0}'); r.ping()" 2>/dev/null; do
  sleep 1
done
echo "Redis ready"

echo "Running migrations..."
python manage.py migrate --noinput

echo "Starting: $SERVICE_TYPE"
case "$SERVICE_TYPE" in
  web)
    python manage.py collectstatic --noinput
    exec gunicorn webhook.wsgi:application \
      --bind 0.0.0.0:8000 \
      --workers 4 \
      --timeout 60 \
      --keep-alive 5 \
      --access-logfile - \
      --error-logfile -
    ;;
  worker)
    exec celery -A webhook worker \
      --loglevel=info \
      --concurrency=${CELERY_CONCURRENCY:-8}
    ;;
  beat)
    exec celery -A webhook beat --loglevel=info --schedule=/tmp/celerybeat-schedule
    ;;
  *)
    echo "ERROR: SERVICE_TYPE must be: web | worker | beat"
    exit 1
    ;;
esac