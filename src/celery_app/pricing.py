import asyncio

from src.celery_app import app
from src.services import platform_manager


@app.task()
def update_pricing_data():
    asyncio.run(platform_manager.update_pricing_data())
