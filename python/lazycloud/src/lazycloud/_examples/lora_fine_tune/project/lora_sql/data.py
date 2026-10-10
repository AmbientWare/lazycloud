"""Download the base model and build the training and evaluation sets on a CPU."""

import random
import shutil
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from itertools import islice
from pathlib import Path

from pydantic import BaseModel

from .queries import QueryError, SchemaError, run_query
from .resources import BASE_MODEL, BASE_MODEL_DIR, DATA_DIR, app, data_image, storage

DATASET = "gretelai/synthetic_text_to_sql"
SPLIT_FILES = {
    "train": "synthetic_text_to_sql_train.snappy.parquet",
    "eval": "synthetic_text_to_sql_test.snappy.parquet",
}
# Questions answered by a query; the dataset also asks for inserts and updates.
QUERY_TASKS = {"analytics and reporting", "data retrieval"}
MAX_CONTEXT_CHARS = 2000
MAX_QUESTION_CHARS = 500
SHUFFLE_SEED = 7
BASE_MODEL_READY = BASE_MODEL_DIR / ".complete"


class SqlExample(BaseModel):
    question: str
    context: str
    sql: str
    complexity: str


class PreparedData(BaseModel):
    train_examples: int
    eval_examples: int


@app.function(
    image=data_image,
    volumes=[storage],
    cpu=2,
    memory="4Gi",
    timeout_seconds=1800,
    retries=2,
)
def prepare_data(train_size: int = 10_000, eval_size: int = 500) -> PreparedData:
    if not 100 <= train_size <= 50_000:
        raise ValueError("train_size must be between 100 and 50000")
    if not 50 <= eval_size <= 2_000:
        raise ValueError("eval_size must be between 50 and 2000")
    _download_base_model()
    for split, size in (("train", train_size), ("eval", eval_size)):
        examples = list(islice(usable_examples(_read_split(split)), size))
        if len(examples) < size:
            raise ValueError(f"the {split} split has only {len(examples)} usable examples")
        write_examples(DATA_DIR / f"{split}.jsonl", examples)
        print(f"wrote {len(examples)} {split} examples", flush=True)
    return PreparedData(train_examples=train_size, eval_examples=eval_size)


def usable_examples(rows: Iterable[Mapping[str, str]]) -> Iterator[SqlExample]:
    """The rows whose reference query runs on SQLite and returns data.

    The dataset mixes SQL dialects. Keeping what SQLite runs teaches the model
    the dialect it is scored and used against.
    """
    for row in rows:
        if (
            row["sql_task_type"] not in QUERY_TASKS
            or len(row["sql_context"]) > MAX_CONTEXT_CHARS
            or len(row["sql_prompt"]) > MAX_QUESTION_CHARS
        ):
            continue
        try:
            result = run_query(row["sql_context"], row["sql"])
        except (SchemaError, QueryError):
            continue
        if all(cell is None for result_row in result.rows for cell in result_row):
            continue
        yield SqlExample(
            question=row["sql_prompt"],
            context=row["sql_context"],
            sql=row["sql"],
            complexity=row["sql_complexity"],
        )


def write_examples(path: Path, examples: Iterable[SqlExample]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.part")
    with partial.open("w", encoding="utf-8") as file:
        for example in examples:
            file.write(example.model_dump_json() + "\n")
    partial.replace(path)


def read_examples(path: Path) -> list[SqlExample]:
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing; run lora_sql.data:prepare_data first")
    with path.open(encoding="utf-8") as file:
        return [SqlExample.model_validate_json(line) for line in file]


def require_base_model() -> None:
    if not BASE_MODEL_READY.exists():
        raise FileNotFoundError(f"{BASE_MODEL_DIR} is missing; run lora_sql.data:prepare_data first")


def _download_base_model() -> None:
    from huggingface_hub import snapshot_download

    if BASE_MODEL_READY.exists():
        return
    # Download to local disk first: the hub's file locks need a local filesystem.
    with tempfile.TemporaryDirectory() as local:
        snapshot_download(
            BASE_MODEL, local_dir=local, allow_patterns=["*.json", "*.safetensors", "*.txt"]
        )
        shutil.copytree(
            local, BASE_MODEL_DIR, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".cache")
        )
    BASE_MODEL_READY.touch()


def _read_split(split: str) -> list[dict[str, str]]:
    import pyarrow.parquet as parquet
    from huggingface_hub import hf_hub_download

    path = hf_hub_download(DATASET, SPLIT_FILES[split], repo_type="dataset")
    columns = ["sql_prompt", "sql_context", "sql", "sql_task_type", "sql_complexity"]
    rows: list[dict[str, str]] = parquet.read_table(path, columns=columns).to_pylist()
    random.Random(SHUFFLE_SEED).shuffle(rows)
    return rows
