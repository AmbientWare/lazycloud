import asyncio

from machines.celery_app import app
from machines.services import platform_manager


@app.task()
def update_pricing_data():
    asyncio.run(platform_manager.update_pricing_data())
