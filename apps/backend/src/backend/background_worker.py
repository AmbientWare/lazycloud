"""Background worker for Prefect task execution.

This module serves the Prefect task workers that handle background tasks
like deployments, deletions, restarts, etc. It runs as a separate service
independent of the API server.
"""

from backend.prefect_app import serve_background_tasks


def main():
    """Serve the Prefect task worker for background tasks."""
    serve_background_tasks()


if __name__ == "__main__":
    main()
