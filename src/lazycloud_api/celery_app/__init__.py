from celery import Celery

from lazycloud_api.config import app_config

app = Celery("tasks", broker=app_config.REDIS_URL)

# Configure minimal Celery settings
app.conf.update(
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    result_backend=app_config.REDIS_URL,
    worker_pool="threads",  # Use threads instead of processes for I/O bound tasks
    worker_concurrency=100,  # Number of concurrent worker threads
    worker_prefetch_multiplier=1,  # prefetch 1 task at a time, but still allow for concurrency
)

# NOTE: Import tasks to ensure they are registered THIS IS REQUIRED
from lazycloud_api.celery_app import pricing, usage, crons, machinees, volumes
