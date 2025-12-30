import logging
import sys
from typing import Any

from loguru import logger

logger.remove()

LOG_FORMAT = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>\n{exception}"


class HealthCheckFilter(logging.Filter):
    """Filter out health check endpoint logs"""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage().find("/health") == -1


class InterceptHandler(logging.Handler):
    """Intercept standard logging and route to loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back  # type: ignore
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


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
                    "backtrace": True,
                    "diagnose": True,
                },
                {
                    "sink": "logs/app.log",
                    "format": LOG_FORMAT,
                    "level": "DEBUG",
                    "rotation": "500 MB",
                    "retention": "10 days",
                    "compression": "zip",
                    "backtrace": True,
                    "diagnose": True,
                },
            ]
        }

    # Remove any existing handlers
    logger.remove()

    # Add handlers from config
    for handler in config["handlers"]:
        logger.add(**handler)

    # Intercept uvicorn loggers and route through loguru
    for name in ["uvicorn", "uvicorn.error", "uvicorn.access"]:
        logging_logger = logging.getLogger(name)
        logging_logger.handlers = [InterceptHandler()]
        logging_logger.setLevel(logging.INFO)
        logging_logger.propagate = False

    # Filter out health check logs
    logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())

    logger.info("Logger configured successfully")
