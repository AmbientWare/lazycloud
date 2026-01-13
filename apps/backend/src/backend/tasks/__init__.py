"""SAQ-based task queue for LazyCloud backend.

This module replaces Prefect for background task execution with SAQ (Simple Async Queue).
"""

from backend.tasks.utils import get_task_result

__all__ = [
    "get_task_result",
]
