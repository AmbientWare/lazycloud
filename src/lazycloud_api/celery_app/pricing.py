import asyncio

from lazycloud_api.celery_app import app
from lazycloud_api.services import platform_manager


@app.task()
def update_pricing_data():
    asyncio.run(platform_manager.update_pricing_data())
