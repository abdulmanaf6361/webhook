# # Gevent monkey-patch for Gunicorn gevent workers.
# # Gunicorn patches its own workers, but Django's database layer (psycopg2)
# # may be imported by management commands (migrate, collectstatic) before
# # gunicorn spawns workers. This ensures the patch is always applied first.
# try:
#     from gevent import monkey
#     monkey.patch_all(thread=True, ssl=True)
# except ImportError:
#     pass  # gevent not installed — fine for non-gevent environments

from .celery import app as celery_app

__all__ = ('celery_app',)