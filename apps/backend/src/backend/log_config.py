import logging
import sys
from typing import Any

from loguru import logger

logger.remove()

LOG_FORMAT = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"


class HealthCheckFilter(logging.Filter):
    """Filter out health check endpoint logs"""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage().find("/health") == -1


def setup_logger(config: dict[str, Any] | None = None) -> None:
    """Setup logger with custom configuration"""
    if config is None:
        config = {
            "handlers": [
                {
                    "sink": sys.stdout,
                    "format": LOG_FORMAT,
                    "level": "INFO",
                    "colorize": True,
                },
                {
                    "sink": "logs/app.log",
                    "format": LOG_FORMAT,
                    "level": "DEBUG",
                    "rotation": "500 MB",
                    "retention": "10 days",
                    "compression": "zip",
                },
            ]
        }

    # Remove any existing handlers
    logger.remove()

    # Add handlers from config
    for handler in config["handlers"]:
        logger.add(**handler)

    # Filter out health check logs from uvicorn access logger
    logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())

    logger.info("Logger configured successfully")
