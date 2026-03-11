#!/bin/bash
set -e

echo "⏳ Waiting for Redis..."

until python -c "import redis; r=redis.from_url('${REDIS_URL:-redis://redis:6379/0}'); r.ping()" 2>/dev/null; do
  sleep 1
done

echo "✅ Redis is ready"
echo "🚀 Starting: $SERVICE_TYPE"

case "$SERVICE_TYPE" in

  web)
    echo "📦 Running migrations..."
    python manage.py migrate --noinput

    echo "📁 Collecting static files..."
    python manage.py collectstatic --noinput

    echo "🌐 Starting Gunicorn..."
    exec gunicorn webhook.wsgi:application \
        --bind 0.0.0.0:8000 \
        --workers 3 \
        --threads 2 \
        --timeout 60 \
        --access-logfile - \
        --error-logfile -
    ;;

  worker)
    echo "⚙️ Starting Celery Worker..."
    exec celery -A webhook worker \
        --loglevel=info \
        --concurrency=4
    ;;

  beat)
    echo "⏱ Starting Celery Beat..."
    exec celery -A webhook beat \
        --loglevel=info \
        --schedule /data/celerybeat-schedule
    ;;

  *)
    echo "❌ ERROR: SERVICE_TYPE must be one of: web, worker, beat"
    exit 1
    ;;

esac