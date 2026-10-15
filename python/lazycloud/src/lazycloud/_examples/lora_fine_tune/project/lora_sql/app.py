"""Every workload of the project in one app, for ``uv run lazycloud deploy lora_sql.app:app``."""

from .compare import compare
from .data import prepare_data
from .evaluate import evaluate, generate_sql, score_batch
from .resources import app
from .serve import sql_server
from .train import train

__all__ = [
    "app",
    "compare",
    "evaluate",
    "generate_sql",
    "prepare_data",
    "score_batch",
    "sql_server",
    "train",
]
