# In your Celery configuration, you can specify schedule:
from celery.schedules import crontab

from lazycloud_api.celery_app import app

app.conf.beat_schedule = {
    "update-pricing-data-every-day-at-midnight": {
        "task": "celery_app.pricing.update_pricing_data",
        "schedule": crontab(hour=0, minute=0),
    },
    "record-customer-usage-every-minute": {
        "task": "celery_app.usage.record_customer_usage",
        "schedule": crontab(minute="*"),
    },
}
