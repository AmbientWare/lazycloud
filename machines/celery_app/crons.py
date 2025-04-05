# In your Celery configuration, you can specify schedule:
from celery.schedules import crontab

from machines.celery_app import app

app.conf.beat_schedule = {
    "update-pricing-data-every-day-at-midnight": {
        "task": "machines.celery_app.pricing.update_pricing_data",
        "schedule": crontab(hour=0, minute=0),
    },
    "record-customer-usage-every-minute": {
        "task": "machines.celery_app.usage.record_customer_usage",
        "schedule": crontab(minute="*"),
    },
}
