#!/bin/bash
set -e
mkdir static
echo "⏳ Waiting for Redis..."
until python -c "import redis; r=redis.from_url('${REDIS_URL:-redis://redis:6379/0}'); r.ping()" 2>/dev/null; do
  sleep 1
done
echo "✅ Redis is ready"

echo "📦 Running migrations..."
python manage.py migrate --noinput

echo "🚀 Starting: $SERVICE_TYPE"
case "$SERVICE_TYPE" in
  web)
    exec python manage.py runserver 0.0.0.0:8000
    ;;
  worker)
    exec celery -A webhook worker --loglevel=info --concurrency=4
    ;;
  beat)
    exec celery -A webhook beat --loglevel=info --schedule /data/celerybeat-schedule
    ;;
  *)
    echo "ERROR: SERVICE_TYPE must be one of: web, worker, beat"
    exit 1
    ;;
esac
