import os
import sys

from dotenv import load_dotenv
from loguru import logger

LOG_FILE = "/tmp/lazycloud-cli.log"


def configure_logging() -> None:
    """Configure logging based on LAZYCLOUD_DEBUG environment variable.

    Set LAZYCLOUD_DEBUG=1 to enable debug file logging.
    Logs are written to /tmp/lazycloud-cli.log and cleared on each session.

    Usage:
        # Add to .env file in workspace root:
        LAZYCLOUD_DEBUG=1

        # Watch logs in another terminal:
        uv run cli-logs
    """
    # Load .env file from current directory or parent directories
    load_dotenv()

    enable_debug = os.environ.get("LAZYCLOUD_DEBUG", "").lower() in ("1", "true")

    if enable_debug:
        # Remove default stderr handler to avoid cluttering the terminal
        logger.remove()

        # Add file handler, clearing previous content
        logger.add(
            LOG_FILE,
            format="{time:HH:mm:ss.SSS} | {level:<8} | {name}:{function}:{line} | {message}",
            level="DEBUG",
            mode="w",  # Overwrite file on each session
            rotation=None,  # Don't rotate - fresh file each session
        )

        logger.info("Debug logging enabled - writing to /tmp/lazycloud-cli.log")
    else:
        # Disable all logging by default (loguru logs to stderr otherwise)
        logger.remove()
        logger.add(sys.stderr, level="ERROR")


def watch_logs() -> None:
    """Watch CLI logs in real-time using tail -f.

    Usage: uv run cli-logs
    """
    import subprocess

    print(f"Watching {LOG_FILE}...")
    print("Press Ctrl+C to stop\n")

    try:
        subprocess.run(["tail", "-f", LOG_FILE], check=True)
    except KeyboardInterrupt:
        print("\nStopped watching logs.")
    except FileNotFoundError:
        print(f"Error: {LOG_FILE} not found. Run the CLI first to create the log file.")
