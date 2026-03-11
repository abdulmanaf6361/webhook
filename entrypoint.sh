#!/bin/bash
set -e

# ── Wait for Redis ─────────────────────────────────────────────────────────────
echo "⏳ Waiting for Redis..."
until python -c "import redis; r=redis.from_url('${REDIS_URL:-redis://redis:6379/0}'); r.ping()" 2>/dev/null; do
  sleep 1
done
echo "✅ Redis ready"

echo "📦 Running migrations..."
python manage.py migrate --noinput

echo "🚀 Starting: $SERVICE_TYPE"
case "$SERVICE_TYPE" in
  web)
    python manage.py collectstatic --noinput
    # gevent worker class: each of the 4 workers spawns 1000 greenlets.
    # All Postgres I/O is non-blocking via django-db-geventpool.
    # 4 workers × 1000 greenlets = handles large bursts without request queuing.
    exec gunicorn webhook.wsgi:application \
      --bind 0.0.0.0:8000 \
      --worker-class gevent \
      --workers 4 \
      --worker-connections 1000 \
      --timeout 60 \
      --keep-alive 5 \
      --access-logfile - \
      --error-logfile -
    ;;

  worker)
    # gevent pool: 1000 concurrent greenlets on 4 cores.
    # Each greenlet yields while waiting on HTTP POST or Postgres — not CPU-bound.
    # -O fair: use gevent's fair scheduling to prevent starvation.
    exec celery -A webhook worker \
      -P gevent \
      --concurrency=1000 \
      --loglevel=info \
      --prefetch-multiplier=1 \
      -O fair
    ;;

  beat)
    exec celery -A webhook beat --loglevel=info
    ;;

  *)
    echo "ERROR: SERVICE_TYPE must be one of: web, worker, beat"
    exit 1
    ;;
esac