import asyncio

from machines.celery_app import app
from machines.services.pricing_manager import PricingManager


@app.task()
def update_pricing_data():
    pricing_manager = PricingManager()
    asyncio.run(pricing_manager.update_pricing_data())
