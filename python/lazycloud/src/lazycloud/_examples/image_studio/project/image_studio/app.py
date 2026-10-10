"""The whole studio: `uv run lazycloud deploy image_studio.app` deploys every
workload the web app and the cleanup schedule import."""

from image_studio import cleanup, web
from image_studio.resources import app

__all__ = ["app", "cleanup", "web"]
